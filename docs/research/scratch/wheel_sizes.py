import json, urllib.request
for pkg in ["PySide6_Essentials","PySide6_Addons","shiboken6","PyInstaller","nuitka","keyboard","pynput","pywebview"]:
    d=json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json"))
    v=d["info"]["version"]
    for f in d["urls"]:
        if "win_amd64" in f["filename"] or f["filename"].endswith("none-any.whl"):
            print(pkg,v,f["filename"],round(f["size"]/1e6,1),"MB",f["upload_time"][:10]); break
