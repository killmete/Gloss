import time, argostranslate.package as p, argostranslate.translate as t
p.update_package_index()
pk=[x for x in p.get_available_packages() if x.from_code=="zh" and x.to_code=="en"][0]
t0=time.time(); path=pk.download(); p.install_from_path(path); print("dl+install s",round(time.time()-t0,1))
import os;print("model MB",round(os.path.getsize(path)/1e6,1))
lines=["哈哈哈你也太搞笑了吧","老板大气!刷个火箭支持一下","今天不开播了，晚安宝子们","这个单怎么接？要先付款吗","我先去打团，等我五分钟","主播好帅啊，求带飞","你是不是在逗我，笑死","已下单，请查收","666 太强了","别划水了，快来开黑","这家伙太菜了，又躺赢","姐姐能加个微信吗","充值入口在哪里","接单大厅","我的订单","设置"]
t.translate("你好","zh","en")
t0=time.time()
for l in lines: print(l,"=>",t.translate(l,"zh","en"))
print("ms/line",round((time.time()-t0)/len(lines)*1000))
