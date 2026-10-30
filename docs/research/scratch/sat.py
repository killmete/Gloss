import time, psutil, numpy as np, mss, json
p=psutil.Process(); s=mss.MSS(); m=s.monitors[1]
for _ in range(3): s.grab(m)
c0=sum(p.cpu_times()[:2]); t0=time.perf_counter(); n=0
while time.perf_counter()-t0<5:
    np.asarray(s.grab(m)); n+=1
w=time.perf_counter()-t0
print(json.dumps({"mss_full_saturated_fps":round(n/w,1),"cpu_pct_of_one_core":round((sum(p.cpu_times()[:2])-c0)/w*100,1)}))
