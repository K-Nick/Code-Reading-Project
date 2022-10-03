import json

# import joblib
import dill
import pickle
import csv
import os
import subprocess
import time
from .general import get_logger

_pjoin = os.path.join
log = get_logger(__name__)

def safe_save_or_load(mode="load"):
    def out_wrapper(op_func):
        def wrapper(*kargs, **kwargs):
            fn = None
            if "fn" in kwargs:
                fn = kwargs["fn"]
            else:
                fn = kargs[-1]

            run_fn = fn + ".run"
            log.info(f"using safe i/o on {fn}")

            if mode == "load":
                if os.path.exists(run_fn):
                    while os.path.exists(run_fn):  # wait until save is finished
                        time.sleep(1)
                ret = op_func(*kargs, **kwargs)
            else:
                if os.path.exists(run_fn):
                    log.warn("save simultinously from other process, skipping save")
                    return

                subprocess.run(f"touch {run_fn}", shell=True)
                ret = op_func(*kargs, **kwargs)
                subprocess.run(f"rm -rf {run_fn}", shell=True)
            
            return ret
                
        return wrapper
    return out_wrapper

def load_json(fn):
    with open(fn, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(obj: dict, fn):
    with open(fn, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=4)


def load_pickle(fn):
    # return joblib.load(fn)
    with open(fn, "rb") as f:
        obj = dill.load(f)
    return obj

def save_pickle(obj, fn):
    # return joblib.dump(obj, fn, protocol=pickle.HIGHEST_PROTOCOL)
    with open(fn, "wb") as f:
        dill.dump(obj, f, protocol=dill.HIGHEST_PROTOCOL)


def run_or_load(func, file_path, cache=True, overwrite=False, args=dict()):
    # def run_or_loader_wrapper(**kwargs):
    #     if os.path.exists()
    #     return fn(**kwargs)
    # if os.path.exists(file_path)
    if overwrite or not os.path.exists(file_path):
        obj = func(**args)
        if cache:
            save_pickle(obj, file_path)
    else:
        obj = load_pickle(obj)

    return obj


def load_csv(fn, delimiter=",", has_header=True):
    fr = open(fn, "r")
    read_csv = csv.reader(fr, delimiter=delimiter)

    ret_list = []

    for idx, x in enumerate(read_csv):
        if has_header and idx == 0:
            header = x
            continue
        if has_header:
            ret_list += [{k: v for k, v in zip(header, x)}]
        else:
            ret_list += [x]

    return ret_list

