import time, os, ctranslate2, sentencepiece as spm
from huggingface_hub import hf_hub_download
d=None
for f in ["model.bin","source_vocabulary.json","target_vocabulary.json","config.json","src.spm.model","tgt.spm.model"]:
    d=os.path.dirname(hf_hub_download("quickmt/quickmt-zh-en",f))
sp=spm.SentencePieceProcessor(os.path.join(d,"src.spm.model")); tp=spm.SentencePieceProcessor(os.path.join(d,"tgt.spm.model"))
lines=["哈哈哈你也太搞笑了吧","老板大气!刷个火箭支持一下","今天不开播了，晚安宝子们","这个单怎么接？要先付款吗","我先去打团，等我五分钟","主播好帅啊，求带飞","你是不是在逗我，笑死","已下单，请查收","666 太强了","别划水了，快来开黑","这家伙太菜了，又躺赢","姐姐能加个微信吗","充值入口在哪里","接单大厅","我的订单","设置"]
for ct in ("default","int8"):
    tr=ctranslate2.Translator(d,device="cpu",compute_type=ct,inter_threads=1,intra_threads=4)
    toks=[sp.encode(l,out_type=str) for l in lines]
    tr.translate_batch(toks[:1],beam_size=2)
    t0=time.time(); out=[]
    for t in toks: out.append(tp.decode(tr.translate_batch([t],beam_size=2)[0].hypotheses[0]))
    ms=(time.time()-t0)/len(lines)*1000
    print(ct,"ms/line",round(ms))
    if ct=="int8":
        for l,o in zip(lines,out): print(l,"=>",o)
