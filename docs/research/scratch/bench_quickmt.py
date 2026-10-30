"""Throwaway: CPU latency of quickmt-zh-en (CTranslate2 int8) on short chat-like lines.
Run: uv run --with ctranslate2 --with sentencepiece --with huggingface_hub python -I bench_quickmt.py
"""
import json
import os
import statistics
import time

import ctranslate2
import sentencepiece as spm
from huggingface_hub import snapshot_download

d = snapshot_download("quickmt/quickmt-zh-en", ignore_patterns=["eole-model/*"])
print("files:", sorted(os.listdir(d)))
print("model.bin MB:", round(os.path.getsize(os.path.join(d, "model.bin")) / 1e6))

LINES = [
    "老板晚上好，有空开黑吗？",
    "姐姐今天不上线吗",
    "收到订单，请稍等一下哦",
    "520 我爱你",
    "可以加个微信吗",
    "麻烦打个五星好评谢谢",
    "这局我带你上分",
    "我先去吃饭，等下回来",
    "订单已完成，感谢老板的支持",
    "刚才网卡了，不好意思",
    "个人资料", "我的钱包", "充值", "联系客服", "接单中",
    "欢迎来到我的直播间，点点关注不迷路",
    "老板大气，送一个火箭",
    "今晚打王者还是吃鸡",
    "语音房里有人吗",
    "再来一把，别走啊",
]
t0 = time.perf_counter()
sp_src = spm.SentencePieceProcessor(os.path.join(d, "src.spm.model"))
sp_tgt = spm.SentencePieceProcessor(os.path.join(d, "tgt.spm.model"))
tr = ctranslate2.Translator(d, device="cpu", compute_type="int8", inter_threads=1, intra_threads=4)
load_s = time.perf_counter() - t0
print("load s:", round(load_s, 2))

def run(lines, beam):
    toks = [sp_src.encode(x, out_type=str) for x in lines]
    res = tr.translate_batch(toks, beam_size=beam)
    return [sp_tgt.decode(r.hypotheses[0]) for r in res]

run(LINES[:2], 2)  # warm
out = {}
for beam in (1, 5):
    single = []
    for ln in LINES:
        t = time.perf_counter()
        run([ln], beam)
        single.append((time.perf_counter() - t) * 1000)
    t = time.perf_counter()
    outs = run(LINES, beam)
    batch_ms = (time.perf_counter() - t) * 1000
    out[beam] = dict(single_median_ms=round(statistics.median(single), 1),
                     single_max_ms=round(max(single), 1),
                     batch20_total_ms=round(batch_ms, 1))
    if beam == 5:
        for a, b in zip(LINES, outs):
            print(a, "->", b)
print(json.dumps(out, indent=1))
json.dump(dict(load_s=load_s, results=out, cpu_threads=4),
          open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "quickmt_bench.json"), "w"), indent=1)
