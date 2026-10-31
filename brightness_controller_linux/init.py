#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# This file is part of Brightness Controller.
#
# Brightness Controller is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Brightness Controller is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Brightness Controller.  If not, see
# <http://www.gnu.org/licenses/>.

import getpass
import os
from os import makedirs, path, remove
import subprocess
import sys
import threading

# Force Qt5 to prefer native Wayland, falling back to X11 (xcb) if needed
os.environ.setdefault("QT_QPA_PLATFORM", "wayland;xcb")

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import QSize, Qt
from PyQt5.QtGui import QIcon

try:
    from brightness_controller_linux.util.QtSingleApplication import QtSingleApplication
    from brightness_controller_linux.ui.mainwindow import Ui_MainWindow
    from brightness_controller_linux.ui.license import Ui_Form as License_Ui_Form
    from brightness_controller_linux.ui.about import Ui_Form as About_Ui_Form
    from brightness_controller_linux.ui.help import Ui_Form as Help_Ui_Form
    from brightness_controller_linux.util import executor as Executor
    from brightness_controller_linux.util import check_displays as CDisplay
    from brightness_controller_linux.util import write_config as WriteConfig
    from brightness_controller_linux.util import read_config as ReadConfig
    from brightness_controller_linux.util import resource_provider as rp
except ImportError:
    from util.QtSingleApplication import QtSingleApplication
    from ui.mainwindow import Ui_MainWindow
    from ui.license import Ui_Form as License_Ui_Form
    from ui.about import Ui_Form as About_Ui_Form
    from ui.help import Ui_Form as Help_Ui_Form
    import util.executor as Executor
    import util.check_displays as CDisplay
    import util.write_config as WriteConfig
    import util.read_config as ReadConfig
    import util.resource_provider as rp


class MyApplication(QtWidgets.QMainWindow):

    def __assign_displays(self):
        """Assigns display names."""
        self.displays = CDisplay.detect_display_devices()
        self.no_of_displays = len(self.displays)
        self.no_of_connected_dev = self.no_of_displays

        if self.no_of_displays == 1:
            self.display1 = self.displays[0]
        elif self.no_of_displays >= 2:
            self.display1 = self.displays[0]
            self.display2 = self.displays[1]

    def _safe_val(self, idx):
        """Safely map a slider integer (0-100) to self.values (indices 0-99)."""
        clamped = max(0, min(len(self.values) - 1, int(idx)))
        return self.values[clamped]

    def directlySetMaxBrightness(self, displayNum, percentage):
        percentage = round(percentage) / 100
        subprocess.run(
            [
                "ddcutil",
                "setvcp",
                "10",
                str(int(self.displayMaxes[displayNum - 1] * percentage)),
                "-d",
                str(displayNum),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def __init__(self, parent=None):
        """Initializes."""
        QtWidgets.QMainWindow.__init__(self, parent)

        self.ddcutil_Installed = False
        self.displayMaxes = []
        self.displayValues = []
        self.displayNames = []

        self.tray_menu = None
        self.tray_icon = None
        self.display1 = None
        self.display2 = None
        self.license_widget = None
        self.about_widget = None
        self.help_widget = None

        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        self.ui_icon = QIcon()
        self.APP = None

        # Check if ddcutil is installed and configured
        try:
            ver_out = str(
                subprocess.check_output(
                    ["ddcutil", "--version"], stderr=subprocess.DEVNULL
                ),
                "utf-8",
            )
            if "ddcutil" in ver_out:
                env_out = str(
                    subprocess.check_output(
                        ["ddcutil", "environment"], stderr=subprocess.DEVNULL
                    ),
                    "utf-8",
                )
                if "sudo modprobe" in env_out:
                    self.ui.ddcutilsNotInstalled.setText(
                        "add i2c-dev to etc/modules-load.d"
                    )
                else:
                    self.ddcutil_Installed = True
        except Exception:
            self.ddcutil_Installed = False

        if self.ddcutil_Installed:
            try:
                getNames = str(
                    subprocess.check_output(
                        ["ddcutil", "detect"], stderr=subprocess.DEVNULL
                    ),
                    "utf-8",
                ).split("\n")

                for line in getNames:
                    if "Model:" in line:
                        model_name = line.split(":", 1)[1].strip()
                        if model_name != "":
                            self.displayNames.append(model_name)
                    if "Invalid display" in line:
                        self.displayNames.append(line.strip())

                for i, name in enumerate(self.displayNames):
                    if name != "Invalid display":
                        brightnessValue = str(
                            subprocess.check_output(
                                ["ddcutil", "getvcp", "10", "-d", str(i + 1)],
                                stderr=subprocess.DEVNULL,
                            ),
                            "utf-8",
                        )
                        self.displayMaxes.append(
                            int(brightnessValue.split(",")[1].split("=")[1].strip())
                        )
                        self.displayValues.append(
                            int(brightnessValue.split(",")[0].split("=")[1].strip())
                        )
                    else:
                        self.displayMaxes.append(1)
                        self.displayValues.append(1)
            except Exception as e:
                print("error: " + str(e))

        self.ui_icon.addFile(rp.icon_path(), QSize(), QIcon.Normal, QIcon.Off)
        self.setWindowIcon(self.ui_icon)
        self.temperature = "Default"
        self.no_of_connected_dev = 0
        self.__assign_displays()
        self.setup_default_directory()
        self.generate_dynamic_items()
        self.default_config = "/home/{}/.config/brightness_controller/settings".format(
            getpass.getuser()
        )
        self.values = []
        self.array_value = 0.01
        for _ in range(0, 100):
            self.values.append(round(self.array_value, 2))
            self.array_value += 0.01
        self.connect_handlers()
        self.setup_widgets()

        if path.exists(self.default_config):
            self.load_settings(self.default_config)

        if self.ddcutil_Installed and self.displayNames:
            res = all(ele == "Invalid display" for ele in self.displayNames)
            if not res:
                self.ui.directControlBox.setEnabled(True)
                self.ui.ddcutilsNotInstalled.setVisible(False)
            else:
                self.ui.ddcutilsNotInstalled.setText(
                    "Laptop Displays Not Supported"
                )

        self.canCloseToTray = False

        if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            self.canCloseToTray = True
            self.setup_tray(parent)

    def setup_default_directory(self):
        """Create default settings directory if it doesnt exist."""
        directory = "/home/{}/.config/brightness_controller/".format(
            getpass.getuser()
        )
        if not path.exists(directory):
            try:
                makedirs(directory)
            except OSError as e:
                self._show_error(str(e))

    def closeEvent(self, event):
        """Override CloseEvent for system tray."""
        if not self.isVisible():
            reply = QtWidgets.QMessageBox.question(
                self,
                "Message",
                "Are you sure to quit?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if reply == QtWidgets.QMessageBox.Yes:
                event.accept()
                sys.exit(self.APP.exec_())
            else:
                event.ignore()
            return
        else:
            self.show()
            self.hide()
            event.ignore()

    def trayClose(self):
        reply = QtWidgets.QMessageBox.question(
            self,
            "Message",
            "Are you sure to quit?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if reply == QtWidgets.QMessageBox.Yes:
            sys.exit(self.APP.exec_())

    def setup_tray(self, parent):
        """Setup system tray."""
        self.tray_menu = QtWidgets.QMenu(parent)

        show_action = QtWidgets.QAction(
            "Show", self, statusTip="Show", triggered=self.show
        )
        quit_action = QtWidgets.QAction(
            "Quit", self, statusTip="Quit", triggered=self.trayClose
        )
        self.tray_menu.addAction(show_action)
        self.tray_menu.addAction(quit_action)

        icon = QtGui.QIcon()
        icon_path = rp.icon_path()
        icon.addPixmap(
            QtGui.QPixmap(icon_path), QtGui.QIcon.Normal, QtGui.QIcon.Off
        )

        self.tray_icon = QtWidgets.QSystemTrayIcon(icon, self)
        self.tray_icon.activated.connect(self._icon_activated)
        self.tray_icon.setContextMenu(self.tray_menu)
        self.tray_icon.show()

    def _icon_activated(self, reason):
        if reason in (
            QtWidgets.QSystemTrayIcon.Trigger,
            QtWidgets.QSystemTrayIcon.DoubleClick,
        ):
            self.show()

    def setup_widgets(self):
        """Connects the form widgets with functions."""
        self.license_widget = LicenseForm()
        self.license_widget.set_main_window(self)
        self.license_widget.hide()

        self.about_widget = AboutForm()
        self.about_widget.set_main_window(self)
        self.about_widget.hide()

        self.help_widget = HelpForm()
        self.help_widget.set_main_window(self)
        self.help_widget.hide()

    def generate_dynamic_items(self):
        """Manages widgets that may contain dynamic items."""
        self.generate_brightness_sources()

    def generate_brightness_sources(self):
        """Generates and assigns display sources to combo boxes."""
        if self.no_of_connected_dev < 2:
            self.ui.secondary_combo.addItem("Disabled")
            self.ui.secondary_combo.setEnabled(False)
            self.ui.primary_combobox.addItem("Disabled")
            self.ui.primary_combobox.setEnabled(False)
            return

        if self.ddcutil_Installed and len(self.displayNames) >= self.no_of_connected_dev:
            for i in range(self.no_of_connected_dev):
                self.ui.secondary_combo.addItem(self.displayNames[i])
                self.ui.primary_combobox.addItem(self.displayNames[i])
        else:
            for display in self.displays:
                self.ui.secondary_combo.addItem(display)
                self.ui.primary_combobox.addItem(display)

    def connect_handlers(self):
        """Connects the handlers of GUI widgets."""
        self.ui.primary_brightness.setTracking(False)
        self.ui.primary_brightness.valueChanged[int].connect(
            self.change_value_pbr
        )
        self.ui.primary_red.valueChanged[int].connect(self.change_value_pr)
        self.ui.primary_blue.valueChanged[int].connect(self.change_value_pb)
        self.ui.primary_green.valueChanged[int].connect(self.change_value_pg)
        self.ui.directControlBox.stateChanged.connect(self.directControlUpdate)
        self.enable_secondary_widgets(False)

        if self.no_of_connected_dev >= 2:
            self.enable_secondary_widgets(True)
            self.connect_secondary_widgets()
            self.ui.secondary_combo.setCurrentIndex(1)

        if path.exists(self.default_config):
            self.ui.actionClearDefault.setVisible(True)
            self.ui.actionClearDefault.triggered.connect(
                self.delete_default_settings
            )

        self.ui.actionDefault.triggered.connect(
            lambda: self.save_settings(True)
        )

        self.ui.comboBox.activated[str].connect(self.combo_activated)
        self.ui.primary_combobox.activated[str].connect(
            self.primary_source_combo_activated
        )
        self.ui.secondary_combo.activated[str].connect(
            self.secondary_source_combo_activated
        )
        self.ui.actionAbout.triggered.connect(self.show_about)
        self.ui.actionExit.triggered.connect(self.close)
        self.ui.actionHelp.triggered.connect(self.show_help)
        self.ui.actionLicense.triggered.connect(self.show_license)
        self.ui.actionSave.triggered.connect(self.save_settings)
        self.ui.actionLoad.triggered.connect(self.load_settings)

    def directControlUpdate(self, value):
        if self.ui.directControlBox.isChecked():
            self.ui.primary_brightness.setMaximum(100)
            self.ui.primary_brightness.setValue(
                int(round((self.displayValues[0] / self.displayMaxes[0]) * 100))
            )
            self.ui.primary_brightness.setFocusPolicy(Qt.NoFocus)
            self.ui.primary_brightness.setTracking(False)

            if self.ui.primary_combobox.currentText() == "Invalid display":
                self.ui.primary_brightness.setEnabled(False)

            if self.no_of_displays > 1:
                self.ui.secondary_brightness.setMaximum(100)
                self.ui.secondary_brightness.setValue(
                    int(
                        round(
                            (self.displayValues[1] / self.displayMaxes[1]) * 100
                        )
                    )
                )
                self.ui.secondary_brightness.setFocusPolicy(Qt.NoFocus)
                self.ui.secondary_brightness.setTracking(False)

                if self.ui.secondary_combo.currentText() == "Invalid display":
                    self.ui.secondary_brightness.setEnabled(False)

        else:
            self.ui.primary_brightness.setMaximum(99)
            self.ui.secondary_brightness.setMaximum(99)
            self.ui.primary_brightness.setValue(99)
            self.ui.secondary_brightness.setValue(99)
            self.ui.primary_brightness.setFocusPolicy(Qt.StrongFocus)
            self.ui.primary_brightness.setTracking(True)
            self.ui.secondary_brightness.setFocusPolicy(Qt.StrongFocus)
            self.ui.secondary_brightness.setTracking(True)

            if self.no_of_displays == 1:
                self.ui.primary_brightness.setEnabled(True)
            else:
                self.ui.primary_brightness.setEnabled(True)
                self.ui.secondary_brightness.setEnabled(True)

    def enable_secondary_widgets(self, boolean):
        """Assigns boolean value to setEnabled(boolean)."""
        self.ui.secondary_brightness.setEnabled(boolean)
        self.ui.secondary_blue.setEnabled(boolean)
        self.ui.secondary_red.setEnabled(boolean)
        self.ui.secondary_green.setEnabled(boolean)

    def connect_secondary_widgets(self):
        """Connects the secondary widgets with functions."""
        self.ui.secondary_brightness.setTracking(False)
        self.ui.secondary_brightness.valueChanged[int].connect(
            self.change_value_sbr
        )
        self.ui.secondary_red.valueChanged[int].connect(self.change_value_sr)
        self.ui.secondary_blue.valueChanged[int].connect(self.change_value_sb)
        self.ui.secondary_green.valueChanged[int].connect(self.change_value_sg)

    def change_value_pbr(self):
        """Changes Primary Display Brightness."""
        if self.ui.directControlBox.isChecked():
            idx = self.ui.primary_combobox.currentIndex()
            setValue = threading.Thread(
                target=self.directlySetMaxBrightness,
                args=(idx + 1, self.ui.primary_brightness.value()),
            )
            setValue.start()

            self.displayValues[idx] = int(
                round(
                    self.ui.primary_brightness.value()
                    / 100
                    * self.displayMaxes[idx]
                )
            )
        else:
            value = self.ui.primary_brightness.value()
            cmd_value = (
                "xrandr --output %s --brightness %s --gamma %s:%s:%s"
                % (
                    self.display1,
                    self._safe_val(value),
                    self._safe_val(self.ui.primary_red.value()),
                    self._safe_val(self.ui.primary_green.value()),
                    self._safe_val(self.ui.primary_blue.value()),
                )
            )
            Executor.execute_command(cmd_value)

    def change_value_pr(self, value):
        """Changes Primary Display Red ratio."""
        cmd_value = (
            "xrandr --output %s --brightness %s --gamma %s:%s:%s"
            % (
                self.display1,
                self._safe_val(self.ui.primary_brightness.value()),
                self._safe_val(value),
                self._safe_val(self.ui.primary_green.value()),
                self._safe_val(self.ui.primary_blue.value()),
            )
        )
        Executor.execute_command(cmd_value)

    def change_value_pg(self, value):
        """Changes Primary Display Green ratio."""
        cmd_value = (
            "xrandr --output %s --brightness %s --gamma %s:%s:%s"
            % (
                self.display1,
                self._safe_val(self.ui.primary_brightness.value()),
                self._safe_val(self.ui.primary_red.value()),
                self._safe_val(value),
                self._safe_val(self.ui.primary_blue.value()),
            )
        )
        Executor.execute_command(cmd_value)

    def change_value_pb(self, value):
        """Changes Primary Display Blue ratio."""
        cmd_value = (
            "xrandr --output %s --brightness %s --gamma %s:%s:%s"
            % (
                self.display1,
                self._safe_val(self.ui.primary_brightness.value()),
                self._safe_val(self.ui.primary_red.value()),
                self._safe_val(self.ui.primary_green.value()),
                self._safe_val(value),
            )
        )
        Executor.execute_command(cmd_value)

    def change_value_sbr(self):
        """Changes Secondary Display Brightness."""
        if self.ui.directControlBox.isChecked():
            idx = self.ui.secondary_combo.currentIndex()
            setValue = threading.Thread(
                target=self.directlySetMaxBrightness,
                args=(idx + 1, self.ui.secondary_brightness.value()),
            )
            setValue.start()

            self.displayValues[idx] = int(
                round(
                    self.ui.secondary_brightness.value()
                    / 100
                    * self.displayMaxes[idx]
                )
            )
        else:
            value = self.ui.secondary_brightness.value()
            cmd_value = (
                "xrandr --output %s --brightness %s --gamma %s:%s:%s"
                % (
                    self.display2,
                    self._safe_val(value),
                    self._safe_val(self.ui.secondary_red.value()),
                    self._safe_val(self.ui.secondary_green.value()),
                    self._safe_val(self.ui.secondary_blue.value()),
                )
            )
            Executor.execute_command(cmd_value)

    def change_value_sr(self, value):
        """Changes Secondary Display Red ratio."""
        cmd_value = (
            "xrandr --output %s --brightness %s --gamma %s:%s:%s"
            % (
                self.display2,
                self._safe_val(self.ui.secondary_brightness.value()),
                self._safe_val(value),
                self._safe_val(self.ui.secondary_green.value()),
                self._safe_val(self.ui.secondary_blue.value()),
            )
        )
        Executor.execute_command(cmd_value)

    def change_value_sg(self, value):
        """Changes Secondary Display Green ratio."""
        cmd_value = (
            "xrandr --output %s --brightness %s --gamma %s:%s:%s"
            % (
                self.display2,
                self._safe_val(self.ui.secondary_brightness.value()),
                self._safe_val(self.ui.secondary_red.value()),
                self._safe_val(value),
                self._safe_val(self.ui.secondary_blue.value()),
            )
        )
        Executor.execute_command(cmd_value)

    def change_value_sb(self, value):
        """Changes Secondary Display Blue ratio."""
        cmd_value = (
            "xrandr --output %s --brightness %s --gamma %s:%s:%s"
            % (
                self.display2,
                self._safe_val(self.ui.secondary_brightness.value()),
                self._safe_val(self.ui.secondary_red.value()),
                self._safe_val(self.ui.secondary_green.value()),
                self._safe_val(value),
            )
        )
        Executor.execute_command(cmd_value)

    def changed_state(self, state):
        self.display1, self.display2 = self.display2, self.display1

    def secondary_source_combo_activated(self, text):
        """Assigns combo value to display."""
        self.display2 = self.displays[self.ui.secondary_combo.currentIndex()]
        if self.ui.directControlBox.isChecked():
            if self.ui.secondary_combo.currentText() == "Invalid display":
                self.ui.secondary_brightness.setEnabled(False)
            else:
                self.ui.secondary_brightness.setEnabled(True)
        else:
            self.ui.secondary_brightness.setEnabled(True)

    def primary_source_combo_activated(self, text):
        """Assigns combo value to display."""
        self.display1 = self.displays[self.ui.primary_combobox.currentIndex()]
        if self.ui.directControlBox.isChecked():
            if self.ui.primary_combobox.currentText() == "Invalid display":
                self.ui.primary_brightness.setEnabled(False)
            else:
                self.ui.primary_brightness.setEnabled(True)
        else:
            self.ui.secondary_brightness.setEnabled(True)

    def combo_activated(self, text):
        """Designates values to display and to sliders."""
        self.temperature = text
        presets = {
            "Default": [255, 255, 255],
            "1900K Candle": [255, 147, 41],
            "2600K 40W Tungsten": [255, 197, 143],
            "2850K 100W Tungsten": [255, 214, 170],
            "3200K Halogen": [255, 241, 224],
            "5200K Carbon Arc": [255, 250, 244],
            "5400K High Noon": [255, 255, 251],
            "6000K Direct Sun": [255, 255, 255],
            "7000K Overcast Sky": [201, 226, 255],
            "20000K Clear Blue Sky": [64, 156, 255],
        }
        if text in presets:
            rgb = presets[text]
            self.change_primary_sliders(rgb)
            if self.no_of_connected_dev >= 2:
                self.change_secondary_sliders(rgb)

    def change_primary_sliders(self, rgb):
        """Assigns values to primary display RGB sliders (0-99 range)."""
        slider_r = min(99, int((rgb[0] * 99) / 255))
        slider_g = min(99, int((rgb[1] * 99) / 255))
        slider_b = min(99, int((rgb[2] * 99) / 255))

        self.ui.primary_red.setValue(slider_r)
        self.ui.primary_green.setValue(slider_g)
        self.ui.primary_blue.setValue(slider_b)

    def change_secondary_sliders(self, rgb):
        """Assigns values to secondary display RGB sliders (0-99 range)."""
        slider_r = min(99, int((rgb[0] * 99) / 255))
        slider_g = min(99, int((rgb[1] * 99) / 255))
        slider_b = min(99, int((rgb[2] * 99) / 255))

        self.ui.secondary_red.setValue(slider_r)
        self.ui.secondary_green.setValue(slider_g)
        self.ui.secondary_blue.setValue(slider_b)

    def change_secondary_sliders_in_rgb_0_99(self, br_rgb):
        self.ui.secondary_brightness.setValue(br_rgb[0])
        self.ui.secondary_red.setValue(br_rgb[1])
        self.ui.secondary_green.setValue(br_rgb[2])
        self.ui.secondary_blue.setValue(br_rgb[3])

    def primary_sliders_in_rgb_0_99(self, br_rgb):
        self.ui.primary_brightness.setValue(br_rgb[0])
        self.ui.primary_red.setValue(br_rgb[1])
        self.ui.primary_green.setValue(br_rgb[2])
        self.ui.primary_blue.setValue(br_rgb[3])

    def secondary_sliders_in_rgb_0_99(self, br_rgb):
        self.ui.secondary_brightness.setValue(br_rgb[0])
        self.ui.secondary_red.setValue(br_rgb[1])
        self.ui.secondary_green.setValue(br_rgb[2])
        self.ui.secondary_blue.setValue(br_rgb[3])

    def show_about(self):
        """Shows the About widget."""
        self.about_widget.show()

    def show_license(self):
        """Shows the License widget."""
        self.license_widget.show()

    def show_help(self):
        """Shows the Help Widget."""
        self.help_widget.show()

    def save_settings(self, default=False):
        """Save current primary and secondary display settings."""
        file_path = (
            self.default_config
            if default
            else QtWidgets.QFileDialog.getSaveFileName()[0]
        )
        if len(file_path) > 5:
            if default:
                self.ui.actionClearDefault.setVisible(True)
            try:
                if self.no_of_connected_dev == 1:
                    WriteConfig.write_primary_display(
                        self.return_current_primary_settings(), file_path
                    )
                elif self.no_of_connected_dev >= 2:
                    WriteConfig.write_both_display(
                        self.return_current_primary_settings(),
                        self.return_current_secondary_settings(),
                        file_path,
                    )
            except (PermissionError, OSError):
                self._show_error(
                    "Does not have permission to write file at " + file_path
                )

    def _show_error(self, message):
        """Shows an Error Message."""
        QtWidgets.QMessageBox.critical(self, "Error", message)

    def delete_default_settings(self):
        """Delete default settings."""
        if path.exists(self.default_config):
            try:
                remove(self.default_config)
                self.ui.actionClearDefault.setVisible(False)
            except OSError as e:
                self._show_error(str(e))
        else:
            return False

    def _load_temperature(self, text="Default"):
        """Load current temperature settings."""
        self.temperature = text
        primary_temperature_index = self.ui.comboBox.findText(
            text, QtCore.Qt.MatchFixedString
        )
        if primary_temperature_index >= 0:
            self.ui.comboBox.setCurrentIndex(primary_temperature_index)

    def load_settings(self, location=None):
        """Load current primary and secondary display settings."""
        file_path = location or QtWidgets.QFileDialog.getOpenFileName()[0]
        if path.exists(file_path):
            loaded_settings = ReadConfig.read_configuration(file_path)
            if len(loaded_settings) == 5:
                self._load_temperature(loaded_settings[4])
                self.primary_sliders_in_rgb_0_99(loaded_settings)
            elif len(loaded_settings) == 11:
                if self.no_of_connected_dev == 1:
                    self.primary_sliders_in_rgb_0_99(
                        (
                            loaded_settings[0],
                            loaded_settings[1],
                            loaded_settings[2],
                            loaded_settings[3],
                        )
                    )
                    return
                primary_source = loaded_settings[4]
                secondary_source = loaded_settings[10]
                self._load_temperature(loaded_settings[5])
                primary_combo_index = self.ui.primary_combobox.findText(
                    primary_source, QtCore.Qt.MatchFixedString
                )
                second_combo_index = self.ui.secondary_combo.findText(
                    secondary_source, QtCore.Qt.MatchFixedString
                )
                if primary_combo_index >= 0:
                    self.ui.primary_combobox.setCurrentIndex(
                        primary_combo_index
                    )
                    self.primary_source_combo_activated(primary_source)
                if second_combo_index >= 0:
                    self.ui.secondary_combo.setCurrentIndex(second_combo_index)
                    self.secondary_source_combo_activated(secondary_source)

                self.primary_sliders_in_rgb_0_99(
                    (
                        loaded_settings[0],
                        loaded_settings[1],
                        loaded_settings[2],
                        loaded_settings[3],
                    )
                )
                self.secondary_sliders_in_rgb_0_99(
                    (
                        loaded_settings[6],
                        loaded_settings[7],
                        loaded_settings[8],
                        loaded_settings[9],
                    )
                )

    def return_current_primary_settings(self):
        return [
            self.ui.primary_brightness.value(),
            self.ui.primary_red.value(),
            self.ui.primary_green.value(),
            self.ui.primary_blue.value(),
            self.display1,
            self.temperature,
        ]

    def return_current_secondary_settings(self):
        return [
            self.ui.secondary_brightness.value(),
            self.ui.secondary_red.value(),
            self.ui.secondary_green.value(),
            self.ui.secondary_blue.value(),
            self.display2,
            self.temperature,
        ]


class LicenseForm(QtWidgets.QWidget):
    """License Form widget initialization."""

    def __init__(self, parent=None):
        QtWidgets.QWidget.__init__(self, parent)
        self.ui = License_Ui_Form()
        self.ui.setupUi(self)
        self.main_window = None
        self.entry_id = None

    def set_main_window(self, main_win):
        self.main_window = main_win


class AboutForm(QtWidgets.QWidget):
    """About Form widget initialization."""

    def __init__(self, parent=None):
        QtWidgets.QWidget.__init__(self, parent)
        self.ui = About_Ui_Form()
        self.ui.setupUi(self)
        self.main_window = None
        self.entry_id = None

    def set_main_window(self, main_win):
        self.main_window = main_win


class HelpForm(QtWidgets.QWidget):
    """Help Form widget initialization."""

    def __init__(self, parent=None):
        QtWidgets.QWidget.__init__(self, parent)
        self.ui = Help_Ui_Form()
        self.ui.setupUi(self)
        self.main_window = None
        self.entry_id = None

    def set_main_window(self, main_win):
        self.main_window = main_win


def main():
    UUID = "PHIR-HWOH-MEIZ-AHTA"
    APP = QtSingleApplication(UUID, sys.argv)
    if APP.isRunning():
        sys.exit(0)
    WINDOW = MyApplication()
    WINDOW.APP = APP
    APP.setActivationWindow(WINDOW)
    WINDOW.show()
    sys.exit(APP.exec_())


if __name__ == "__main__":
    main()
