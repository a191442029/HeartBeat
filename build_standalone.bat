@echo off
echo 正在打包独立版本的HRMLink...
pyinstaller HRMLink_standalone.spec --clean
echo 打包完成！
echo 可执行文件位于: dist\HRMLink.exe
