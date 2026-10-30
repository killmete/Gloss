import json, urllib.request
repos=["ramjke/Translumo","HIllya51/LunaTranslator","OneMoreGres/ScreenTranslator","microsoft/PowerToys","zyddnys/manga-image-translator","Artikash/Textractor","bluelovers/screen-translator","tesseract-ocr/tesseract"]
def get(u):
    r=urllib.request.Request(u,headers={"User-Agent":"gloss-research"})
    return json.load(urllib.request.urlopen(r))
out={}
for r in repos:
    try:
        d=get("https://api.github.com/repos/"+r)
        out[r]={k:d.get(k) for k in["stargazers_count","license","pushed_at","open_issues_count","archived","language","created_at"]}
        out[r]["license"]=(d.get("license") or {}).get("spdx_id")
        print(r,out[r])
    except Exception as e: print(r,"ERR",e)
json.dump(out,open("../data/github_repo_stats_2026-10-30.json","w"),indent=1)
