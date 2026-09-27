"""桌宠窗口独立进程入口。"""
import sys

from PyQt5.QtWidgets import QApplication

from desktop.icon_utils import load_app_icon


def run_pet_window():
    """运行桌宠窗口（独立进程）
    
    创建 Qt 应用并显示桌宠窗口。
    """
    from . import PetWindow

    app = QApplication(sys.argv)
    app.setWindowIcon(load_app_icon())
    
    pet = PetWindow()
    pet.show()
    
    sys.exit(app.exec_())


# 边界/有效性检查
if __name__ == "__main__":
    run_pet_window()
