import os

def main(VER2, vname=None):
    if not vname: vname = ".".join(map(str, VER2))
    if os.path.exists("build.bat"):
        with open("build.bat", "w", encoding="utf-8") as f:
            f.write(f"pyinstaller HRMLink.spec --clean --distpath=./_dist/{vname}")
    if os.path.exists("version.txt"):
        with open("version.txt", "w", encoding="utf-8") as f:
            text_ = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({", ".join(map(str, VER2))}),
    prodvers=({", ".join(map(str, VER2))}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404B0',
        [
          StringStruct('FileDescription', '通过低功耗蓝牙协议获取心率并显示 | Compiled using Pyinstaller'),
          StringStruct('ProductVersion', '{", ".join(map(str, VER2[0:3]))}'),
          StringStruct('LegalCopyright', 'Copyright (C) 2026 HRMLink | GPL-3.0 License'),
          StringStruct('CompanyName', 'HRMLink'),
          StringStruct('OriginalFilename', 'HRMLink.exe'),
        ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""
            f.write(text_)
    return None