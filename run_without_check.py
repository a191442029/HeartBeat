import os
import sys
import subprocess

def main():
    """
    启动HRMLink.exe，设置环境变量以跳过重复运行检查
    """
    print("Setting up environment to bypass duplicate running check...")
    
    # 设置环境变量
    os.environ['SKIP_DUPLICATE_CHECK'] = '1'
    
    # 获取当前目录
    current_dir = os.path.dirname(os.path.abspath(__file__))
    
    # 构建HRMLink.exe的路径
    exe_path = os.path.join(current_dir, "dist", "HRMLink.exe")
    
    if not os.path.exists(exe_path):
        print(f"Error: {exe_path} not found!")
        print("Looking for HRMLink.exe in current directory...")
        exe_path = os.path.join(current_dir, "HRMLink.exe")
        
        if not os.path.exists(exe_path):
            print(f"Error: {exe_path} not found!")
            print("Please make sure HRMLink.exe is in the dist folder or current directory.")
            return 1
    
    print(f"Starting {exe_path}...")
    
    # 启动HRMLink.exe
    try:
        subprocess.Popen([exe_path])
        print("HRMLink.exe started successfully!")
    except Exception as e:
        print(f"Error starting HRMLink.exe: {e}")
        return 1
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
