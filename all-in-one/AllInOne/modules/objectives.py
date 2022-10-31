import random

import torch
import torch.nn as nn
import torch.nn.functional as F
import os
import glob
import json
import tqdm
import functools
import itertools
from torch.utils.data.distributed import DistributedSampler
from einops import rearrange

from AllInOne.modules.dist_utils import all_gather
from AllInOne.modules.retrieval_metrics import t2v_metrics, v2t_metrics



def cost_matrix_cosine(x, y, eps=1e-5):
    """Compute cosine distnace across every pairs of x, y (batched)
    [B, L_x, D] [B, L_y, D] -> [B, Lx, Ly]"""
    assert x.dim() == y.dim()
    assert x.size(0) == y.size(0)
    assert x.size(2) == y.size(2)
    x_norm = F.normalize(x, p=2, dim=-1, eps=eps)
    y_norm = F.normalize(y, p=2, dim=-1, eps=eps)
    cosine_sim = x_norm.matmul(y_norm.transpose(1, 2))
    cosine_dist = 1 - cosine_sim
    return cosine_dist


def trace(x):
    """ compute trace of input tensor (batched) """
    b, m, n = x.size()
    assert m == n
    mask = torch.eye(n, dtype=torch.bool, device=x.device).unsqueeze(0).expand_as(x)
    trace = x.masked_select(mask).contiguous().view(b, n).sum(dim=-1, keepdim=False)
    return trace


@torch.no_grad()
def ipot(C, x_len, x_pad, y_len, y_pad, joint_pad, beta, iteration, k):
    """ [B, M, N], [B], [B, M], [B], [B, N], [B, M, N]"""
    b, m, n = C.size()
    sigma = torch.ones(b, m, dtype=C.dtype, device=C.device) / x_len.unsqueeze(1)
    T = torch.ones(b, n, m, dtype=C.dtype, device=C.device)
    A = torch.exp(-C.transpose(1, 2) / beta)

    # mask padded positions
    sigma.masked_fill_(x_pad, 0)
    joint_pad = joint_pad.transpose(1, 2)
    T.masked_fill_(joint_pad, 0)
    A.masked_fill_(joint_pad, 0)

    # broadcastable lengths
    x_len = x_len.unsqueeze(1).unsqueeze(2)
    y_len = y_len.unsqueeze(1).unsqueeze(2)

    # mask to zero out padding in delta and sigma
    x_mask = (x_pad.to(C.dtype) * 1e4).unsqueeze(1)
    y_mask = (y_pad.to(C.dtype) * 1e4).unsqueeze(1)

    for _ in range(iteration):
        Q = A * T  # bs * n * m
        sigma = sigma.view(b, m, 1)
        for _ in range(k):
            delta = 1 / (y_len * Q.matmul(sigma).view(b, 1, n) + y_mask)
            sigma = 1 / (x_len * delta.matmul(Q) + x_mask)
        T = delta.view(b, n, 1) * Q * sigma
    T.masked_fill_(joint_pad, 0)
    return T


def optimal_transport_dist(
    txt_emb, img_emb, txt_pad, img_pad, beta=0.5, iteration=50, k=1
):
    """ [B, M, D], [B, N, D], [B, M], [B, N]"""
    cost = cost_matrix_cosine(txt_emb, img_emb)
    # mask the padded inputs
    joint_pad = txt_pad.unsqueeze(-1) | img_pad.unsqueeze(-2)
    cost.masked_fill_(joint_pad, 0)

    txt_len = (txt_pad.size(1) - txt_pad.sum(dim=1, keepdim=False)).to(dtype=cost.dtype)
    img_len = (img_pad.size(1) - img_pad.sum(dim=1, keepdim=False)).to(dtype=cost.dtype)

    T = ipot(
        cost.detach(), txt_len, txt_pad, img_len, img_pad, joint_pad, beta, iteration, k
    )
    distance = trace(cost.matmul(T.detach()))
    return distance


def compute_mlm(pl_module, batch):
    infer = pl_module.infer(batch, mask_text=True, mask_image=False)
    mlm_logits = pl_module.mlm_score(infer["text_feats"])
    mlm_labels = infer["text_labels"]

    mlm_loss = F.cross_entropy(
        mlm_logits.view(-1, pl_module.hparams.config["vocab_size"]),
        mlm_labels.view(-1),
        ignore_index=-100,
    )

    ret = {
        "mlm_loss": mlm_loss,
        "mlm_logits": mlm_logits,
        "mlm_labels": mlm_labels,
        "mlm_ids": infer["text_ids"],
    }

    phase = "train" if pl_module.training else "val"
    loss = getattr(pl_module, f"{phase}_mlm_loss")(ret["mlm_loss"])
    acc = getattr(pl_module, f"{phase}_mlm_accuracy")(
        ret["mlm_logits"], ret["mlm_labels"]
    )
    pl_module.log(f"mlm/{phase}/loss", loss)
    pl_module.log(f"mlm/{phase}/accuracy", acc)

    return ret


def compute_mpp(pl_module, batch):
    infer = pl_module.infer(batch, mask_text=False, mask_image=True)
    mpp_logits = pl_module.mpp_score(infer["image_feats"])
    mpp_logits = torch.stack(
        [
            mpp_logits[:, :, 0:256],
            mpp_logits[:, :, 256:512],
            mpp_logits[:, :, 512:768],
        ],
        dim=2,
    )
    mpp_labels = infer["image_labels"]

    mpp_loss = F.cross_entropy(
        mpp_logits.view(-1, 256),
        mpp_labels.view(-1),
        ignore_index=-100,
    )

    ret = {
        "mpp_loss": mpp_loss,
        "mpp_logits": mpp_logits,
        "mpp_labels": mpp_labels,
    }

    phase = "train" if pl_module.training else "val"
    loss = getattr(pl_module, f"{phase}_mpp_loss")(ret["mpp_loss"])
    acc = getattr(pl_module, f"{phase}_mpp_accuracy")(
        ret["mpp_logits"], ret["mpp_labels"]
    )
    pl_module.log(f"mpp/{phase}/loss", loss)
    pl_module.log(f"mpp/{phase}/accuracy", acc)

    return ret


def compute_mppd(pl_module, batch):
    infer = pl_module.infer(batch, mask_text=False, mask_image=True)
    mppd_logits = pl_module.mppd_score(infer["image_feats"])
    mppd_labels = infer["image_labels_mppd"]
    filter_to_train = infer["image_labels"].float().mean(dim=-1) != -100

    labels = mppd_labels[filter_to_train]
    logits = mppd_logits[filter_to_train]
    mppd_loss = F.mse_loss(logits, labels)

    ret = {
        "mppd_loss": mppd_loss,
        "mppd_logits": mppd_logits,
        "mppd_labels": mppd_labels,
    }

    phase = "train" if pl_module.training else "val"
    loss = getattr(pl_module, f"{phase}_mppd_loss")(ret["mppd_loss"])
    pl_module.log(f"mppd/{phase}/loss", loss)

    return ret


def compute_mpfr(pl_module, batch):
    infer = pl_module.infer(batch, mask_text=False, mask_image=True)
    mpfr_logits = pl_module.mpfr_score(infer["image_feats"])
    mpfr_labels = infer["image_labels_mpfr"]
    filter_to_train = infer["image_labels"].float().mean(dim=-1) != -100

    labels = mpfr_labels[filter_to_train]
    logits = mpfr_logits[filter_to_train]
    mpfr_loss = F.mse_loss(logits, labels)

    ret = {
        "mpfr_loss": mpfr_loss,
        "mpfr_logits": mpfr_logits,
        "mpfr_labels": mpfr_labels,
    }

    phase = "train" if pl_module.training else "val"
    loss = getattr(pl_module, f"{phase}_mpfr_loss")(ret["mpfr_loss"])
    pl_module.log(f"mpfr/{phase}/loss", loss)

    return ret


def sim_matrix(a, b, eps=1e-8):
    """
    added eps for numerical stability
    """
    a_n, b_n = a.norm(dim=1)[:, None], b.norm(dim=1)[:, None]
    a_norm = a / torch.max(a_n, eps * torch.ones_like(a_n))
    b_norm = b / torch.max(b_n, eps * torch.ones_like(b_n))
    sim_mt = torch.mm(a_norm, b_norm.transpose(0, 1))
    return sim_mt


# ==  add contrastive loss for retrieval
# here not mask text
def compute_itc(pl_module, batch):
    infer = pl_module.infer(batch, mask_text=False, mask_image=False)
    with torch.cuda.amp.autocast(enabled=False):
        txt_emb, img_emb = infer["text_retrieval_feats"], infer["image_retrieval_feats"]
    x = sim_matrix(txt_emb, img_emb)
    temperature = 0.05
    "Assumes input x is similarity matrix of N x M \in [-1, 1], computed using the cosine similarity between normalised vectors"
    i_logsm = F.log_softmax(x / temperature, dim=1)
    j_logsm = F.log_softmax(x.t() / temperature, dim=1)

    # sum over positives
    idiag = torch.diag(i_logsm)
    loss_i = idiag.sum() / len(idiag)

    jdiag = torch.diag(j_logsm)
    loss_j = jdiag.sum() / len(jdiag)

    itc_loss =  - loss_i - loss_j

    ret = {
        "itc_loss": itc_loss,
    }

    phase = "train" if pl_module.training else "val"
    loss = getattr(pl_module, f"{phase}_itc_loss")(ret["itc_loss"])
    pl_module.log(f"itc/{phase}/loss", loss)

    return ret

# == end


# add independent contrastive loss for retrieval

def compute_ind_itc(pl_module, batch):
    infer_text = pl_module.infer(batch, mask_text=False, mask_image=False, input_text_only=True)
    with torch.cuda.amp.autocast(enabled=False):
        txt_emb = infer_text["text_feats"]
    infer_vision = pl_module.infer(batch, mask_text=False, mask_image=False, input_image_only=True)
    with torch.cuda.amp.autocast(enabled=False):
        img_emb = infer_vision["image_feats"]
    # print(txt_emb.size(), img_emb.size())
    x = sim_matrix(txt_emb[:, 0], img_emb[:, 0])
    temperature = 0.05
    "Assumes input x is similarity matrix of N x M \in [-1, 1], computed using the cosine similarity between normalised vectors"
    i_logsm = F.log_softmax(x / temperature, dim=1)
    j_logsm = F.log_softmax(x.t() / temperature, dim=1)

    # sum over positives
    idiag = torch.diag(i_logsm)
    loss_i = idiag.sum() / len(idiag)

    jdiag = torch.diag(j_logsm)
    loss_j = jdiag.sum() / len(jdiag)

    itc_loss =  - loss_i - loss_j

    ret = {
        "ind_itc_loss": itc_loss,
    }

    phase = "train" if pl_module.training else "val"
    loss = getattr(pl_module, f"{phase}_ind_itc_loss")(ret["ind_itc_loss"])
    pl_module.log(f"ind_itc/{phase}/loss", loss)

    return ret

# == end

def compute_itm_wpa(pl_module, batch):
    pos_len = len(batch["text"]) // 2
    neg_len = len(batch["text"]) - pos_len
    itm_labels = torch.cat([torch.ones(pos_len), torch.zeros(neg_len)]).to(
        pl_module.device
    )
    itm_labels = itm_labels[torch.randperm(itm_labels.size(0))]

    itm_images = [
        torch.stack(
            [
                ti if itm_labels[i] == 1 else fi
                for i, (ti, fi) in enumerate(zip(bti, bfi))
            ]
        )
        for bti, bfi in zip(batch["image"], batch["false_image_0"])
    ]

    batch = {k: v for k, v in batch.items()}
    batch["image"] = itm_images

    infer = pl_module.infer(batch, mask_text=False, mask_image=False)

    with torch.cuda.amp.autocast(enabled=False):
        txt_emb, img_emb = infer["text_feats"], infer["image_feats"]
        txt_mask, img_mask = infer["text_masks"].bool(), infer["image_masks"].bool()
        for i, _len in enumerate(txt_mask.sum(dim=1)):
            txt_mask[i, _len - 1] = False
        txt_mask[:, 0] = False
        img_mask[:, 0] = False
        if "deit" in pl_module.hparams.config["vit"]:
            img_mask[:, 1] = False
        txt_pad, img_pad = ~txt_mask, ~img_mask

        cost = cost_matrix_cosine(txt_emb.float(), img_emb.float())
        joint_pad = txt_pad.unsqueeze(-1) | img_pad.unsqueeze(-2)
        cost.masked_fill_(joint_pad, 0)

        txt_len = (txt_pad.size(1) - txt_pad.sum(dim=1, keepdim=False)).to(
            dtype=cost.dtype
        )
        img_len = (img_pad.size(1) - img_pad.sum(dim=1, keepdim=False)).to(
            dtype=cost.dtype
        )
        T = ipot(
            cost.detach(), txt_len, txt_pad, img_len, img_pad, joint_pad, 0.5, 50, 1
        )
        distance = trace(cost.matmul(T.detach()))

    dist_pos = distance.masked_select(itm_labels == 1)
    dist_neg = distance.masked_select(itm_labels == 0)
    ot_loss = (dist_pos.sum() - dist_neg.sum()) / (dist_pos.size(0) + dist_neg.size(0))

    itm_logits = pl_module.itm_score(infer["cls_feats"])
    itm_loss = F.cross_entropy(itm_logits, itm_labels.long())

    ret = {
        "itm_loss": itm_loss,
        "itm_wpa_loss": 0.1 * ot_loss,
        "itm_logits": itm_logits,
        "itm_labels": itm_labels,
    }

    phase = "train" if pl_module.training else "val"
    loss = getattr(pl_module, f"{phase}_itm_loss")(ret["itm_loss"])
    wpa_loss = getattr(pl_module, f"{phase}_itm_wpa_loss")(ret["itm_wpa_loss"])
    acc = getattr(pl_module, f"{phase}_itm_accuracy")(
        ret["itm_logits"], ret["itm_labels"]
    )
    pl_module.log(f"itm/{phase}/loss", loss)
    pl_module.log(f"itm/{phase}/wpa_loss", wpa_loss)
    pl_module.log(f"itm/{phase}/accuracy", acc)

    return ret


def compute_imgcls(pl_module, batch):
    infer = pl_module.infer(batch, mask_text=False, mask_image=False)
    imgcls_logits = pl_module.img_classifier(infer["cls_feats"])
    imgcls_labels = batch["label"]
    imgcls_labels = torch.tensor(imgcls_labels).to(pl_module.device).long()
    imgcls_loss = F.cross_entropy(imgcls_logits, imgcls_labels)

    ret = {
        "imgcls_loss": imgcls_loss,
        "imgcls_logits": imgcls_logits,
        "imgcls_labels": imgcls_labels,
    }

    phase = "train" if pl_module.training else "val"
    loss = getattr(pl_module, f"{phase}_imgcls_loss")(ret["imgcls_loss"])
    acc = getattr(pl_module, f"{phase}_imgcls_accuracy")(
        ret["imgcls_logits"], ret["imgcls_labels"]
    )
    pl_module.log(f"imgcls/{phase}/loss", loss)
    pl_module.log(f"imgcls/{phase}/accuracy", acc)

    return ret


# vcr q -> a
def compute_vcr_q2a(pl_module, batch):
    false_len = pl_module.hparams.config["draw_options_text"] - 1
    itm_labels = torch.tensor(batch["answer"]).to(pl_module.device).long()
    _bs, _t, _c, _h, _w = batch["image"][0].shape
    # for qa
    text_ids = torch.stack(
        [batch[f"options_text_{i}_ids"] for i in range(false_len)], dim=1
    )
    text_masks = torch.stack(
        [batch[f"options_text_{i}_masks"] for i in range(false_len)], dim=1
    )
    text_labels = torch.stack(
        [batch[f"options_text_{i}_labels"] for i in range(false_len)], dim=1
    )

    # concat first option and other options
    text_ids = torch.cat([batch["text_ids"].unsqueeze(1), text_ids], dim=1)
    text_masks = torch.cat([batch["text_masks"].unsqueeze(1), text_masks], dim=1)
    text_labels = torch.cat([batch["text_labels"].unsqueeze(1), text_labels], dim=1)
    images = batch["image"][0].unsqueeze(1).expand(_bs, false_len + 1, _t, _c, _h, _w)

    infer = pl_module.infer(
        {
            "image": [rearrange(images, "bs fs t c h w -> (bs fs) t c h w")],
            "text_ids": rearrange(text_ids, "bs fs tl -> (bs fs) tl"),
            "text_masks": rearrange(text_masks, "bs fs tl -> (bs fs) tl"),
            "text_labels": rearrange(text_labels, "bs fs tl -> (bs fs) tl"),
        }
    )
    score = pl_module.rank_output(infer["cls_feats"])[:, 0]
    score = rearrange(score, "(bs fs) -> bs fs", bs=_bs, fs=false_len + 1)
    qa_loss = F.cross_entropy(score, itm_labels)
    # for qa->r

    reason_len = pl_module.hparams.config["draw_options_text"]
    qar_labels = torch.tensor(batch["reason_answer"]).to(pl_module.device).long()
    _bs, _t, _c, _h, _w = batch["image"][0].shape
    # for qar
    qar_text_ids = torch.stack(
        [batch[f"qar_text_{i}_ids"] for i in range(reason_len)], dim=1
    )
    qar_text_masks = torch.stack(
        [batch[f"qar_text_{i}_masks"] for i in range(reason_len)], dim=1
    )
    qar_text_labels = torch.stack(
        [batch[f"qar_text_{i}_labels"] for i in range(reason_len)], dim=1
    )

    # concat first option and other options
    images = batch["image"][0].unsqueeze(1).expand(_bs, reason_len, _t, _c, _h, _w)

    qar_infer = pl_module.infer(
        {
            "image": [rearrange(images, "bs fs t c h w -> (bs fs) t c h w")],
            "text_ids": rearrange(qar_text_ids, "bs fs tl -> (bs fs) tl"),
            "text_masks": rearrange(qar_text_masks, "bs fs tl -> (bs fs) tl"),
            "text_labels": rearrange(qar_text_labels, "bs fs tl -> (bs fs) tl"),
        }
    )
    qar_score = pl_module.rank_output_2(qar_infer["cls_feats"])[:, 0]
    qar_score = rearrange(qar_score, "(bs fs) -> bs fs", bs=_bs, fs=reason_len)
    qar_loss = F.cross_entropy(qar_score, qar_labels)

    # print(score, itm_labels)
    phase = "train" if pl_module.training else "val"
    qa_acc = getattr(pl_module, f"{phase}_vcr_q2a_accuracy")(
        score, itm_labels
    )
    qar_acc = getattr(pl_module, f"{phase}_vcr_qar_accuracy")(
        qar_score, qar_labels
    )

    ret = {
        "vcr_q2a_loss": qa_loss,
        "vcr_qar_loss": qar_loss
    }

    phase = "train" if pl_module.training else "val"
    qa_loss = getattr(pl_module, f"{phase}_vcr_q2a_loss")(ret["vcr_q2a_loss"])
    qar_loss = getattr(pl_module, f"{phase}_vcr_qar_loss")(ret["vcr_qar_loss"])

    pl_module.log(f"vcr_q2a/{phase}/loss", qa_loss)
    pl_module.log(f"vcr_qar/{phase}/loss", qar_loss)
    pl_module.log(f"vcr_q2a/{phase}/accuracy", qa_acc)
    pl_module.log(f"vcr_qar/{phase}/accuracy", qar_acc)
    return ret


# vcr qa -> r
def compute_vcr_qa2r(pl_module, batch):
    false_len = pl_module.hparams.config["draw_false_text"] - 1
    # stack image multiple times
    # print(batch["answer"])
    itm_labels = torch.tensor(batch["answer"]).to(pl_module.device).long()
    _bs, _t, _c, _h, _w = batch["image"][0].shape
    # print(batch.keys())

    text_ids = torch.stack(
        [batch[f"false_text_{i}_ids"] for i in range(false_len)], dim=1
    )
    text_masks = torch.stack(
        [batch[f"false_text_{i}_masks"] for i in range(false_len)], dim=1
    )
    text_labels = torch.stack(
        [batch[f"false_text_{i}_labels"] for i in range(false_len)], dim=1
    )

    # concat first option and other options
    text_ids = torch.cat([batch["text_ids"].unsqueeze(1), text_ids], dim=1)
    text_masks = torch.cat([batch["text_masks"].unsqueeze(1), text_masks], dim=1)
    text_labels = torch.cat([batch["text_labels"].unsqueeze(1), text_labels], dim=1)
    images = batch["image"][0].unsqueeze(1).expand(_bs, false_len + 1, _t, _c, _h, _w)

    infer = pl_module.infer(
        {
            "image": [rearrange(images, "bs fs t c h w -> (bs fs) t c h w")],
            "text_ids": rearrange(text_ids, "bs fs tl -> (bs fs) tl"),
            "text_masks": rearrange(text_masks, "bs fs tl -> (bs fs) tl"),
            "text_labels": rearrange(text_labels, "bs fs tl -> (bs fs) tl"),
        }
    )
    score = pl_module.rank_output(infer["cls_feats"])[:, 0]
    score = rearrange(score, "(bs fs) -> bs fs", bs=_bs, fs=false_len + 1)
    loss = F.cross_entropy(score, itm_labels)

    # print(score, itm_labels)

    phase = "train" if pl_module.training else "val"
    acc = getattr(pl_module, f"{phase}_multiple_choice_accuracy")(
        score, itm_labels
    )

    ret = {
        "multiple_choice_loss": loss,
    }

    phase = "train" if pl_module.training else "val"
    loss = getattr(pl_module, f"{phase}_multiple_choice_loss")(ret["multiple_choice_loss"])

    pl_module.log(f"multiple_choice/{phase}/loss", loss)
    pl_module.log(f"multiple_choice/{phase}/accuracy", acc)
    return ret


# mc_vqa
def compute_mc_vqa_q2a(pl_module, batch):
    false_len = pl_module.hparams.config["draw_options_text"] - 1
    itm_labels = torch.tensor(batch["answer"]).to(pl_module.device).long()
    _bs, _t, _c, _h, _w = batch["image"][0].shape
    # for qa
    text_ids = torch.stack(
        [batch[f"options_text_{i}_ids"] for i in range(false_len)], dim=1
    )
    text_masks = torch.stack(
        [batch[f"options_text_{i}_masks"] for i in range(false_len)], dim=1
    )
    text_labels = torch.stack(
        [batch[f"options_text_{i}_labels"] for i in range(false_len)], dim=1
    )

    # concat first option and other options
    text_ids = torch.cat([batch["text_ids"].unsqueeze(1), text_ids], dim=1)
    text_masks = torch.cat([batch["text_masks"].unsqueeze(1), text_masks], dim=1)
    text_labels = torch.cat([batch["text_labels"].unsqueeze(1), text_labels], dim=1)
    images = batch["image"][0].unsqueeze(1).expand(_bs, false_len + 1, _t, _c, _h, _w)

    infer = pl_module.infer(
        {
            "image": [rearrange(images, "bs fs t c h w -> (bs fs) t c h w")],
            "text_ids": rearrange(text_ids, "bs fs tl -> (bs fs) tl"),
            "text_masks": rearrange(text_masks, "bs fs tl -> (bs fs) tl"),
            "text_labels": rearrange(text_labels, "bs fs tl -> (bs fs) tl"),
        }
    )
    ##  v0: use rank output
    # score = pl_module.rank_output(infer["cls_feats"])[:, 0]
    ## v1: use classification head
    # print(infer["cls_feats"].size()) # 40, 768
    score = pl_module.mc_vqa_classifier(infer["cls_feats"])[:, 0]
    score = rearrange(score, "(bs fs) -> bs fs", bs=_bs, fs=false_len + 1)
    qa_loss = F.cross_entropy(score, itm_labels)
    # print(score, itm_labels)
    phase = "train" if pl_module.training else "val"
    qa_acc = getattr(pl_module, f"{phase}_mc_vqa_accuracy")(
        score, itm_labels
    )
    ret = {
        "mc_vqa_loss": qa_loss,
    }

    phase = "train" if pl_module.training else "val"
    qa_loss = getattr(pl_module, f"{phase}_mc_vqa_loss")(ret["mc_vqa_loss"])
    pl_module.log(f"mc_vqa/{phase}/loss", qa_loss)
    pl_module.log(f"mc_vqa/{phase}/accuracy", qa_acc)
    return ret


# msrvtt multiple choice
def compute_multiple_choice(pl_module, batch):
    false_len = pl_module.hparams.config["draw_false_text"] - 1
    # stack image multiple times
    # print(batch["answer"])
    itm_labels = torch.tensor(batch["answer"]).to(pl_module.device).long()
    _bs, _t, _c, _h, _w = batch["image"][0].shape
    # print(batch.keys())

    text_ids = torch.stack(
        [batch[f"false_text_{i}_ids"] for i in range(false_len)], dim=1
    )
    text_masks = torch.stack(
        [batch[f"false_text_{i}_masks"] for i in range(false_len)], dim=1
    )
    text_labels = torch.stack(
        [batch[f"false_text_{i}_labels"] for i in range(false_len)], dim=1
    )

    # concat first option and other options
    text_ids = torch.cat([batch["text_ids"].unsqueeze(1), text_ids], dim=1)
    text_masks = torch.cat([batch["text_masks"].unsqueeze(1), text_masks], dim=1)
    text_labels = torch.cat([batch["text_labels"].unsqueeze(1), text_labels], dim=1)
    images = batch["image"][0].unsqueeze(1).expand(_bs, false_len + 1, _t, _c, _h, _w)

    infer = pl_module.infer(
        {
            "image": [rearrange(images, "bs fs t c h w -> (bs fs) t c h w")],
            "text_ids": rearrange(text_ids, "bs fs tl -> (bs fs) tl"),
            "text_masks": rearrange(text_masks, "bs fs tl -> (bs fs) tl"),
            "text_labels": rearrange(text_labels, "bs fs tl -> (bs fs) tl"),
        }
    )
    score = pl_module.rank_output(infer["cls_feats"])[:, 0]
    score = rearrange(score, "(bs fs) -> bs fs", bs=_bs, fs=false_len + 1)
    loss = F.cross_entropy(score, itm_labels)

    # print(score, itm_labels)

    phase = "train" if pl_module.training else "val"
    acc = getattr(pl_module, f"{phase}_multiple_choice_accuracy")(
        score, itm_labels
    )
    # print(acc)
    ret = {
        "multiple_choice_loss": loss,
    }

    phase = "train" if pl_module.training else "val"
    loss = getattr(pl_module, f"{phase}_multiple_choice_loss")(ret["multiple_choice_loss"])

    pl_module.log(f"multiple_choice/{phase}/loss", loss)
    pl_module.log(f"multiple_choice/{phase}/accuracy", acc)
    return ret


def compute_vqa(pl_module, batch):
    infer = pl_module.infer(batch, mask_text=False, mask_image=False)
    vqa_logits = pl_module.vqa_classifier(infer["cls_feats"])
    vqa_targets = torch.zeros(
        len(vqa_logits), pl_module.hparams.config["vqav2_label_size"]
    ).to(pl_module.device)

    vqa_labels = batch["vqa_labels"]
    vqa_scores = batch["vqa_scores"]

    for i, (_label, _score) in enumerate(zip(vqa_labels, vqa_scores)):
        for l, s in zip(_label, _score):
            vqa_targets[i, l] = s

    vqa_loss = (
        F.binary_cross_entropy_with_logits(vqa_logits, vqa_targets)
        * vqa_targets.shape[1]
    )  # https://github.com/jnhwkim/ban-vqa/blob/master/train.py#L19

    ret = {
        "vqa_loss": vqa_loss,
        "vqa_logits": vqa_logits,
        "vqa_targets": vqa_targets,
        "vqa_labels": vqa_labels,
        "vqa_scores": vqa_scores,
    }

    phase = "train" if pl_module.training else "val"
    loss = getattr(pl_module, f"{phase}_vqa_loss")(ret["vqa_loss"])
    score = getattr(pl_module, f"{phase}_vqa_score")(
        ret["vqa_logits"], ret["vqa_targets"]
    )
    pl_module.log(f"vqa/{phase}/loss", loss)
    pl_module.log(f"vqa/{phase}/score", score)

    return ret


# add by vcop
def compute_vcop(pl_module, batch):
    infer = pl_module.infer(batch, mask_text=False, mask_image=False)
    x = infer["vcop_features"]  # BTLC
    b = x.size(0)
    # # v1: simple concat
    # gt_labels = torch.ones(b)
    # idx = torch.randperm(pl_module.hparams.config["num_frames"])  # get random order
    # classes = list(itertools.permutations(list(range(len(idx.tolist())))))
    # label = classes.index(tuple(idx.tolist()))
    # h = x[0, idx, 0].view(1, -1)
    # gt_labels[0] = label
    # for index in range(1, b):
    #     idx = torch.randperm(pl_module.hparams.config["num_frames"])  # get random order
    #     classes = list(itertools.permutations(list(range(len(idx.tolist())))))
    #     label = classes.index(tuple(idx.tolist()))
    #     gt_labels[index] = label
    #     h = torch.cat((h, x[index, idx, 0].view(1, -1)), dim=0)

    # v2: vcop implementation
    gt_labels = torch.ones(b)
    idx = torch.randperm(pl_module.hparams.config["num_frames"])  # get random order
    classes = list(itertools.permutations(list(range(len(idx.tolist())))))
    label = classes.index(tuple(idx.tolist()))
    h = x[0, idx, 0].unsqueeze(0)
    gt_labels[0] = label
    for index in range(1, b):
        idx = torch.randperm(pl_module.hparams.config["num_frames"])  # get random order
        classes = list(itertools.permutations(list(range(len(idx.tolist())))))
        label = classes.index(tuple(idx.tolist()))
        gt_labels[index] = label
        h = torch.cat((h, x[index, idx, 0].unsqueeze(0)), dim=0)
    # print(h.size())
    # print(classes, label)
    # print(idx)
    # print(h.size())
    vcop_logits = pl_module.vcop_classifier(h)
    vcop_labels = gt_labels.to(pl_module.device).long()
    m = nn.Softmax(dim=1)
    if random.random() < 0.1:
        print(m(vcop_logits)[0], vcop_labels[0])
    # print(vcop_labels)
    vcop_loss = F.cross_entropy(vcop_logits, vcop_labels)
    ret = {
        "vcop_loss": vcop_loss,
        "vcop_logits": vcop_logits,
        "vcop_labels": vcop_labels,
    }
    phase = "train" if pl_module.training else "val"
    loss = getattr(pl_module, f"{phase}_vcop_loss")(ret["vcop_loss"])
    pl_module.log(f"vcop/{phase}/loss", loss)
    # print(ret["vqa_logits"])
    # print(ret["vqa_labels"])
    # print(ret["vqa_logits"].size(), vqa_labels.size())
    acc = getattr(pl_module, f"{phase}_vcop_accuracy")(
        ret["vcop_logits"], ret["vcop_labels"], unfilterd=False  # if remove unknown classes
    )
    pl_module.log(f"vcop/{phase}/accuracy", acc)
    return ret


# add by msrvtt qa
def compute_openend_vqa(pl_module, batch):
    phase = "train" if pl_module.training else "val"
    infer = pl_module.infer(batch, mask_text=False, mask_image=False, mode=phase)
    vqa_logits = pl_module.vqa_classifier(infer["cls_feats"])
    vqa_labels = torch.tensor(batch["vqa_labels"]).to(pl_module.device).long()
    # print(vqa_logits.size())
    # print(vqa_labels)
    vqa_loss = F.cross_entropy(vqa_logits, vqa_labels)
    ret = {
        "vqa_loss": vqa_loss,
        "vqa_logits": vqa_logits,
        "vqa_labels": vqa_labels,
    }
    loss = getattr(pl_module, f"{phase}_vqa_loss")(ret["vqa_loss"])
    pl_module.log(f"vqa/{phase}/loss", loss)
    # print(ret["vqa_logits"])
    # print(ret["vqa_labels"])
    # print(ret["vqa_logits"].size(), vqa_labels.size())
    acc = getattr(pl_module, f"{phase}_openend_vqa_accuracy")(
        ret["vqa_logits"], ret["vqa_labels"], unfilterd=False  # if remove unknown classes
    )
    pl_module.log(f"vqa/{phase}/accuracy", acc)
    return ret


def compute_nlvr2(pl_module, batch):
    infer1 = pl_module.infer(
        batch, mask_text=False, mask_image=False, image_token_type_idx=1
    )
    infer2 = pl_module.infer(
        batch, mask_text=False, mask_image=False, image_token_type_idx=2
    )

    cls_feats = torch.cat([infer1["cls_feats"], infer2["cls_feats"]], dim=-1)
    nlvr2_logits = pl_module.nlvr2_classifier(cls_feats)

    nlvr2_labels = batch["answers"]
    nlvr2_labels = torch.tensor(nlvr2_labels).to(pl_module.device).long()
    nlvr2_loss = F.cross_entropy(nlvr2_logits, nlvr2_labels)

    ret = {
        "nlvr2_loss": nlvr2_loss,
        "nlvr2_logits": nlvr2_logits,
        "nlvr2_labels": nlvr2_labels,
    }

    phase = "train" if pl_module.training else "val"

    if phase == "train":
        loss = getattr(pl_module, f"{phase}_nlvr2_loss")(ret["nlvr2_loss"])
        acc = getattr(pl_module, f"{phase}_nlvr2_accuracy")(
            ret["nlvr2_logits"], ret["nlvr2_labels"]
        )
        pl_module.log(f"nlvr2/{phase}/loss", loss)
        pl_module.log(f"nlvr2/{phase}/accuracy", acc)
    else:
        dev_batches = [i for i, n in enumerate(batch["table_name"]) if "dev" in n]
        test_batches = [i for i, n in enumerate(batch["table_name"]) if "test" in n]

        if dev_batches:
            dev_loss = getattr(pl_module, f"dev_nlvr2_loss")(
                F.cross_entropy(
                    ret["nlvr2_logits"][dev_batches], ret["nlvr2_labels"][dev_batches]
                )
            )
            dev_acc = getattr(pl_module, f"dev_nlvr2_accuracy")(
                ret["nlvr2_logits"][dev_batches], ret["nlvr2_labels"][dev_batches]
            )
            pl_module.log(f"nlvr2/dev/loss", dev_loss)
            pl_module.log(f"nlvr2/dev/accuracy", dev_acc)
        if test_batches:
            test_loss = getattr(pl_module, f"test_nlvr2_loss")(
                F.cross_entropy(
                    ret["nlvr2_logits"][test_batches], ret["nlvr2_labels"][test_batches]
                )
            )
            test_acc = getattr(pl_module, f"test_nlvr2_accuracy")(
                ret["nlvr2_logits"][test_batches], ret["nlvr2_labels"][test_batches]
            )
            pl_module.log(f"nlvr2/test/loss", test_loss)
            pl_module.log(f"nlvr2/test/accuracy", test_acc)

    return ret


def compute_irtr(pl_module, batch):
    is_training_phase = pl_module.training
    # modify to module
    _bs, _t, _c, _h, _w = batch["image"][0].shape
    false_len = pl_module.hparams.config["draw_false_text"]
    text_ids = torch.stack(
        [batch[f"false_text_{i}_ids"] for i in range(false_len)], dim=1
    )
    text_masks = torch.stack(
        [batch[f"false_text_{i}_masks"] for i in range(false_len)], dim=1
    )
    text_labels = torch.stack(
        [batch[f"false_text_{i}_labels"] for i in range(false_len)], dim=1
    )

    text_ids = torch.cat([batch["text_ids"].unsqueeze(1), text_ids], dim=1)
    text_masks = torch.cat([batch["text_masks"].unsqueeze(1), text_masks], dim=1)
    text_labels = torch.cat([batch["text_labels"].unsqueeze(1), text_labels], dim=1)
    images = batch["image"][0].unsqueeze(1).expand(_bs, false_len + 1, _t, _c, _h, _w)

    infer = pl_module.infer(
        {
            "image": [rearrange(images, "bs fs t c h w -> (bs fs) t c h w")],
            "text_ids": rearrange(text_ids, "bs fs tl -> (bs fs) tl"),
            "text_masks": rearrange(text_masks, "bs fs tl -> (bs fs) tl"),
            "text_labels": rearrange(text_labels, "bs fs tl -> (bs fs) tl"),
        }
    )
    score = pl_module.rank_output(infer["cls_feats"])[:, 0]
    score = rearrange(score, "(bs fs) -> bs fs", bs=_bs, fs=false_len + 1)
    answer = torch.zeros(_bs).to(score).long()
    irtr_loss = F.cross_entropy(score, answer)

    ret = {
        "irtr_loss": irtr_loss,
    }

    phase = "train" if pl_module.training else "val"
    irtr_loss = getattr(pl_module, f"{phase}_irtr_loss")(ret["irtr_loss"])

    pl_module.log(f"irtr/{phase}/irtr_loss", irtr_loss)

    return ret


# use this method to achievt multiple view testing
@torch.no_grad()
def compute_irtr_recall(pl_module):
    text_dset = pl_module.trainer.datamodule.dms[0].make_no_false_val_dset()
    text_dset.tokenizer = pl_module.trainer.datamodule.dms[0].tokenizer
    text_loader = torch.utils.data.DataLoader(
        text_dset,
        batch_size=64,
        num_workers=pl_module.hparams.config["num_workers"],
        pin_memory=True,
        collate_fn=functools.partial(
            text_dset.collate,
            mlm_collator=pl_module.trainer.datamodule.dms[0].mlm_collator,
        ),
    )

    image_dset = pl_module.trainer.datamodule.dms[0].make_no_false_val_dset(
        image_only=True
    )
    image_dset.tokenizer = pl_module.trainer.datamodule.dms[0].tokenizer
    dist_sampler = DistributedSampler(image_dset, shuffle=False)
    image_loader = torch.utils.data.DataLoader(
        image_dset,
        batch_size=1,
        num_workers=pl_module.hparams.config["num_workers"],
        sampler=dist_sampler,
        pin_memory=True,
        collate_fn=functools.partial(
            image_dset.collate,
            mlm_collator=pl_module.trainer.datamodule.dms[0].mlm_collator,
        ),
    )

    text_preload = list()
    for _b in tqdm.tqdm(text_loader, desc="text prefetch loop"):
        text_preload.append(
            {
                "text_ids": _b["text_ids"].to(pl_module.device),
                "text_masks": _b["text_masks"].to(pl_module.device),
                "text_labels": _b["text_labels"].to(pl_module.device),
                "img_index": _b["img_index"],
            }
        )

    tiids = list()
    for pre in text_preload:
        tiids += pre["img_index"]
    tiids = torch.tensor(tiids)

    image_preload = list()
    for _b in tqdm.tqdm(image_loader, desc="image prefetch loop"):
        video = _b["image"][0]
        # print(video.size())
        (ie, im, _, _) = pl_module.transformer.visual_embed(
            video.to(pl_module.device),
            max_image_len=pl_module.hparams.config["max_image_len"],
            mask_it=False,
        )
        image_preload.append((ie, im, _b["img_index"][0]))

    rank_scores = list()
    rank_iids = list()

    for img_batch in tqdm.tqdm(image_preload, desc="rank loop"):
        _ie, _im, _iid = img_batch
        num_frames, l, c = _ie.shape

        # print(_ie.size())  # 1x197x168
        # print(_im.size())  # 1x197
        _ie.unsqueeze(0)
        _im.unsqueeze(0)
        img_batch_score = list()
        for txt_batch in text_preload:
            fblen = len(txt_batch["text_ids"])
            ie = _ie.expand(fblen, num_frames, l, c)
            # print(ie.size())
            im = _im.expand(fblen, num_frames, l)
            ie = ie.contiguous().view(-1, l, c)
            im = im.contiguous().view(-1, l)

            with torch.cuda.amp.autocast():
                score = pl_module.rank_output(
                    pl_module.infer(
                        {
                            "text_ids": txt_batch["text_ids"],
                            "text_masks": txt_batch["text_masks"],
                            "text_labels": txt_batch["text_labels"],
                        },
                        image_embeds=ie,
                        image_masks=im,
                    )["cls_feats"]
                )[:, 0]

            img_batch_score.append(score)

        img_batch_score = torch.cat(img_batch_score)
        rank_scores.append(img_batch_score.cpu().tolist())
        rank_iids.append(_iid)

    torch.distributed.barrier()
    gather_rank_scores = all_gather(rank_scores)
    gather_rank_iids = all_gather(rank_iids)

    iids = torch.tensor(gather_rank_iids)
    iids = iids.view(-1)
    scores = torch.tensor(gather_rank_scores)
    scores = scores.view(len(iids), -1)

    topk10 = scores.topk(10, dim=1)
    topk5 = scores.topk(5, dim=1)
    topk1 = scores.topk(1, dim=1)
    topk10_iids = tiids[topk10.indices]
    topk5_iids = tiids[topk5.indices]
    topk1_iids = tiids[topk1.indices]

    tr_r10 = (iids.unsqueeze(1) == topk10_iids).float().max(dim=1)[0].mean()
    tr_r5 = (iids.unsqueeze(1) == topk5_iids).float().max(dim=1)[0].mean()
    tr_r1 = (iids.unsqueeze(1) == topk1_iids).float().max(dim=1)[0].mean()

    topk10 = scores.topk(10, dim=0)
    topk5 = scores.topk(5, dim=0)
    topk1 = scores.topk(1, dim=0)
    topk10_iids = iids[topk10.indices]
    topk5_iids = iids[topk5.indices]
    topk1_iids = iids[topk1.indices]

    ir_r10 = (tiids.unsqueeze(0) == topk10_iids).float().max(dim=0)[0].mean()
    ir_r5 = (tiids.unsqueeze(0) == topk5_iids).float().max(dim=0)[0].mean()
    ir_r1 = (tiids.unsqueeze(0) == topk1_iids).float().max(dim=0)[0].mean()

    return (ir_r1, ir_r5, ir_r10, tr_r1, tr_r5, tr_r10)


@torch.no_grad()
def compute_decouple_irtr_recall(pl_module):
    sample_dset = pl_module.trainer.datamodule.dms[0].make_no_false_val_dset(
    )
    sample_dset.tokenizer = pl_module.trainer.datamodule.dms[0].tokenizer
    dist_sampler = DistributedSampler(sample_dset, shuffle=False)
    sample_loader = torch.utils.data.DataLoader(
        sample_dset,
        batch_size=1,
        num_workers=pl_module.hparams.config["num_workers"],
        sampler=dist_sampler,
        pin_memory=True,
        collate_fn=functools.partial(
            sample_dset.collate,
            mlm_collator=pl_module.trainer.datamodule.dms[0].mlm_collator,
        ),
    )

    text_preload = list()
    text_embed_arr = []
    vid_embed_arr = []
    count = 0
    with torch.no_grad():
        for _b in tqdm.tqdm(sample_loader, desc="text&image prefetch loop"):
            # print(_b)
            # print(_b.keys())
            _b["text_ids"] =  _b["text_ids"].to(pl_module.device)
            _b["text_masks"] =  _b["text_masks"].to(pl_module.device)
            _b["text_labels"] =  _b["text_labels"].to(pl_module.device)
            _b["image"][0] = _b["image"][0].to(pl_module.device)

            infer = pl_module.infer(_b, mask_text=False, mask_image=False)
            with torch.cuda.amp.autocast(enabled=False):
                text_embed, vid_embed = infer["text_retrieval_feats"], infer["image_retrieval_feats"]
                if vid_embed is not None:
                    vid_embed_all = [torch.zeros_like(vid_embed) for _ in range(pl_module.hparams.config["num_gpus"])]
                    torch.distributed.all_gather(vid_embed_all, vid_embed)
                    vid_embed_all = torch.cat(vid_embed_all, dim=0)
                if text_embed is not None:
                    text_embed_all = [torch.zeros_like(text_embed) for _ in range(pl_module.hparams.config["num_gpus"])]
                    torch.distributed.all_gather(text_embed_all, text_embed)
                    text_embed_all = torch.cat(text_embed_all, dim=0)
                text_embed_arr.append(text_embed_all.cpu())
                vid_embed_arr.append(vid_embed_all.cpu())
                count += 1
    text_embeds = torch.cat(text_embed_arr)
    vid_embeds = torch.cat(vid_embed_arr)
    # print(text_embeds.size(), vid_embeds.size())
    st2sv_sims = sim_matrix(text_embeds, vid_embeds).detach().cpu().numpy()
    for metric in [t2v_metrics, v2t_metrics]:
        metric_name = metric.__name__
        metrics = metric(st2sv_sims)
        if metric == t2v_metrics:
            tr_r1, tr_r5, tr_r10, tr_r50 = metrics["R1"], metrics["R5"], metrics["R10"], metrics["R50"]
        else:
            ir_r1, ir_r5, ir_r10, ir_r50 = metrics["R1"], metrics["R5"], metrics["R10"], metrics["R50"]
        # msg += f"MedR: {metrics['MedR']:g}, MeanR: {metrics['MeanR']:.1f}"
    return (ir_r1, ir_r5, ir_r10, tr_r1, tr_r5, tr_r10)


@torch.no_grad()
def compute_zero_shot_classify_recall(pl_module, batch):
    # process all prompt action label into text representations
    false_len = pl_module.hparams.config["draw_false_text"] - 1
    # stack image multiple times
    # print(batch["answer"])
    itm_labels = torch.tensor(batch["answer"]).to(pl_module.device).long()
    _bs, _t, _c, _h, _w = batch["image"][0].shape
    # print(batch.keys())

    text_ids = torch.stack(
        [batch[f"false_text_{i}_ids"] for i in range(false_len)], dim=1
    )
    text_masks = torch.stack(
        [batch[f"false_text_{i}_masks"] for i in range(false_len)], dim=1
    )
    text_labels = torch.stack(
        [batch[f"false_text_{i}_labels"] for i in range(false_len)], dim=1
    )

    # concat first option and other options
    text_ids = torch.cat([batch["text_ids"].unsqueeze(1), text_ids], dim=1)
    text_masks = torch.cat([batch["text_masks"].unsqueeze(1), text_masks], dim=1)
    text_labels = torch.cat([batch["text_labels"].unsqueeze(1), text_labels], dim=1)
    images = batch["image"][0].unsqueeze(1).expand(_bs, false_len + 1, _t, _c, _h, _w)

    infer = pl_module.infer(
        {
            "image": [rearrange(images, "bs fs t c h w -> (bs fs) t c h w")],
            "text_ids": rearrange(text_ids, "bs fs tl -> (bs fs) tl"),
            "text_masks": rearrange(text_masks, "bs fs tl -> (bs fs) tl"),
            "text_labels": rearrange(text_labels, "bs fs tl -> (bs fs) tl"),
        }
    )
    score = pl_module.rank_output(infer["cls_feats"])[:, 0]
    score = rearrange(score, "(bs fs) -> bs fs", bs=_bs, fs=false_len + 1)
    loss = F.cross_entropy(score, itm_labels)

    # print(score, itm_labels)

    phase = "train" if pl_module.training else "val"
    acc = getattr(pl_module, f"{phase}_zero_shot_accuracy")(
        score, itm_labels
    )
    # print(acc)
    ret = {
        "multiple_choice_loss": loss,
    }

    phase = "train" if pl_module.training else "val"
    loss = getattr(pl_module, f"{phase}_multiple_choice_loss")(ret["multiple_choice_loss"])

    pl_module.log(f"multiple_choice/{phase}/loss", loss)
    pl_module.log(f"multiple_choice/{phase}/accuracy", acc)
    return acc


# for ind itc
@torch.no_grad()
def compute_ind_irtr_recall(pl_module):
    num_views = pl_module.hparams.config["retrieval_views"]
    text_embed_arr_multi = []
    vid_embed_arr_multi = []
    for i in range(num_views):
        sample_dset = pl_module.trainer.datamodule.dms[0].make_no_false_val_dset(
        )
        sample_dset.tokenizer = pl_module.trainer.datamodule.dms[0].tokenizer
        dist_sampler = DistributedSampler(sample_dset, shuffle=False)
        sample_loader = torch.utils.data.DataLoader(
            sample_dset,
            batch_size=1,
            num_workers=pl_module.hparams.config["num_workers"],
            sampler=dist_sampler,
            pin_memory=True,
            collate_fn=functools.partial(
                sample_dset.collate,
                mlm_collator=pl_module.trainer.datamodule.dms[0].mlm_collator,
            ),
        )
        text_preload = list()
        text_embed_arr = []
        vid_embed_arr = []
        count = 0
        with torch.no_grad():
            for _b in tqdm.tqdm(sample_loader, desc="text&image prefetch loop"):
                # print(_b)
                # print(_b.keys())
                _b["text_ids"] = _b["text_ids"].to(pl_module.device)
                _b["text_masks"] = _b["text_masks"].to(pl_module.device)
                _b["text_labels"] = _b["text_labels"].to(pl_module.device)
                _b["image"][0] = _b["image"][0].to(pl_module.device)

                # infer = pl_module.infer(_b, mask_text=False, mask_image=False)

                infer_text = pl_module.infer(_b, mask_text=False, mask_image=False, input_text_only=True)
                infer_vision = pl_module.infer(_b, mask_text=False, mask_image=False, input_image_only=True)

                with torch.cuda.amp.autocast(enabled=False):
                    # text_embed, vid_embed = infer_text["raw_cls_feats"], infer_vision["raw_cls_feats"]
                    text_embed, vid_embed = infer_text["text_feats"][:, 0], infer_vision["image_feats"][:, 0]
                    if vid_embed is not None:
                        vid_embed_all = [torch.zeros_like(vid_embed) for _ in range(pl_module.hparams.config["num_gpus"])]
                        torch.distributed.all_gather(vid_embed_all, vid_embed)
                        vid_embed_all = torch.cat(vid_embed_all, dim=0)
                    if text_embed is not None:
                        text_embed_all = [torch.zeros_like(text_embed) for _ in range(pl_module.hparams.config["num_gpus"])]
                        torch.distributed.all_gather(text_embed_all, text_embed)
                        text_embed_all = torch.cat(text_embed_all, dim=0)
                    text_embed_arr.append(text_embed_all.cpu())
                    vid_embed_arr.append(vid_embed_all.cpu())
                    count += 1
        text_embeds = torch.cat(text_embed_arr)
        vid_embeds = torch.cat(vid_embed_arr)
        # append for multi view
        text_embed_arr_multi.append(text_embeds)
        vid_embed_arr_multi.append(vid_embeds)
        # print(text_embeds.size(), vid_embeds.size())
    for j in range(len(text_embed_arr_multi)):
        if j == 0:
            st2sv_sims = sim_matrix(text_embed_arr_multi[j], vid_embed_arr_multi[j]).detach().cpu().numpy() / len(text_embed_arr_multi)
        else:
            st2sv_sims += sim_matrix(text_embed_arr_multi[j], vid_embed_arr_multi[j]).detach().cpu().numpy() / len(text_embed_arr_multi)
    # st2sv_sims = sim_matrix(text_embeds, vid_embeds).detach().cpu().numpy()
    for metric in [t2v_metrics, v2t_metrics]:
        metric_name = metric.__name__
        metrics = metric(st2sv_sims)
        if metric == t2v_metrics:
            tr_r1, tr_r5, tr_r10, tr_r50 = metrics["R1"], metrics["R5"], metrics["R10"], metrics["R50"]
        else:
            ir_r1, ir_r5, ir_r10, ir_r50 = metrics["R1"], metrics["R5"], metrics["R10"], metrics["R50"]
        # msg += f"MedR: {metrics['MedR']:g}, MeanR: {metrics['MeanR']:.1f}"
    return (ir_r1, ir_r5, ir_r10, tr_r1, tr_r5, tr_r10)


def init_weights(module):
    if isinstance(module, (nn.Linear, nn.Embedding)):
        module.weight.data.normal_(mean=0.0, std=0.02)
    elif isinstance(module, nn.LayerNorm):
        module.bias.data.zero_()
        module.weight.data.fill_(1.0)

    if isinstance(module, nn.Linear) and module.bias is not None:
        module.bias.data.zero_()


def vqa_test_step(pl_module, batch, output):
    id2answer = (
        pl_module.trainer.datamodule.dm_dicts["vqa_trainval"].id2answer
        if "vqa_trainval" in pl_module.trainer.datamodule.dm_dicts
        else pl_module.trainer.datamodule.dm_dicts["vqa"].id2answer
    )
    vqa_logits = output["vqa_logits"]
    vqa_preds = vqa_logits.argmax(dim=-1)
    vqa_preds = [id2answer[pred.item()] for pred in vqa_preds]
    questions = batch["text"]
    qids = batch["qid"]
    return {"qids": qids, "preds": vqa_preds}


def openend_vqa_test_step(pl_module, batch, output):
    id2answer = (
        pl_module.trainer.datamodule.dm_dicts["vqa_trainval"].id2answer
        if "vqa_trainval" in pl_module.trainer.datamodule.dm_dicts
        else pl_module.trainer.datamodule.dm_dicts["msrvttqa"].id2answer
    )
    vqa_logits = output["vqa_logits"]
    vqa_preds = vqa_logits.argmax(dim=-1)
    vqa_preds = [id2answer[pred.item()] for pred in vqa_preds]
    questions = batch["text"]
    qids = batch["qid"]
    return {"qids": qids, "preds": vqa_preds}


def arc_test_step(pl_module, batch, output):
    return output


def vqa_test_wrapup(outs, model_name):
    rank = torch.distributed.get_rank()
    qids, preds = list(), list()
    for out in outs:
        qids += out["qids"]
        preds += out["preds"]

    rets = list()
    for qid, pred in zip(qids, preds):
        rets.append({"question_id": qid, "answer": pred})
    with open(f"vqa_submit_{rank}.json", "w") as fp:
        json.dump(rets, fp, indent=4)

    torch.distributed.barrier()

    if rank == 0:
        jsons = list()
        paths = list(glob.glob("vqa_submit_*.json"))
        for path in paths:
            with open(path, "r") as fp:
                jsons += json.load(fp)
        os.makedirs("result", exist_ok=True)
        with open(f"result/vqa_submit_{model_name}.json", "w") as fp:
            json.dump(jsons, fp, indent=4)

    torch.distributed.barrier()
    os.remove(f"vqa_submit_{rank}.json")


def arc_test_wrapup(outs, caplen, model_name):
    rank = torch.distributed.get_rank()
    iids, captions = list(), list()
    for out in outs:
        iids += out["iid"]
        captions += out["captions"]

    rets = list()
    for iid, caption in zip(iids, captions):
        rets.append({"image_id": iid, "caption": caption})
    with open(f"coco_cap_len{caplen}_{rank}.json", "w") as fp:
        json.dump(rets, fp, indent=4)

    torch.distributed.barrier()

    if rank == 0:
        jsons = list()
        paths = list(glob.glob(f"coco_cap_len{caplen}_*.json"))
        for path in paths:
            with open(path, "r") as fp:
                jsons += json.load(fp)
        os.makedirs("result/arc", exist_ok=True)
        jsons = sorted(jsons, key=lambda x: x["image_id"])
        with open(f"result/arc/coco_cap_{model_name}_len{caplen}.json", "w") as fp:
            json.dump(jsons, fp, indent=4)

    torch.distributed.barrier()
    os.remove(f"coco_cap_len{caplen}_{rank}.json")
