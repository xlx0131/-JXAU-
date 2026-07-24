#!/usr/bin/env python3
"""
PyInstaller 打包脚本 - 江西农业大学教务一体化工具
"""
import subprocess
import sys
import os
import time

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)
    
    # 使用完整路径，确保子进程能找到 pyinstaller
    pyi = r"E:\Program Files\Tencent\Marvis\MarvisAgent\1.0.1100.349\runtime\python311\Scripts\pyinstaller.exe"
    
    # PyInstaller 命令
    cmd = [
        pyi,
        "--noconsole",           # 无控制台窗口
        "--onefile",             # 单文件
        "--name", "JXAU教务一体化",
        "--icon", "NONE",        # 无图标
        "--add-data", "jxau_course_grabber.py;.",
        "--add-data", "jxau_grade_query.py;.",
        "--add-data", "jxau_schedule_query.py;.",
        "--collect-all", "ddddocr",
        "--hidden-import", "requests",
        "--hidden-import", "bs4",
        "--hidden-import", "lxml",
        "jxau_jiaowu_gui.py"
    ]
    
    print("开始打包...")
    print("命令:", " ".join(cmd))
    print("-" * 60)
    
    start = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        elapsed = time.time() - start
        
        if result.returncode == 0:
            exe_path = os.path.join(script_dir, "dist", "JXAU教务一体化.exe")
            if os.path.exists(exe_path):
                size_mb = os.path.getsize(exe_path) / (1024 * 1024)
                print(f"\n✅ 打包成功！")
                print(f"  文件: {exe_path}")
                print(f"  大小: {size_mb:.1f} MB")
                print(f"  耗时: {elapsed:.1f} 秒")
            else:
                print(f"\n⚠️  打包完成但未找到 exe 文件")
        else:
            print(f"\n❌ 打包失败 (返回码 {result.returncode})")
            print("标准输出:")
            print(result.stdout[:2000])
            print("\n标准错误:")
            print(result.stderr[:2000])
            
    except Exception as e:
        print(f"\n❌ 执行异常: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()