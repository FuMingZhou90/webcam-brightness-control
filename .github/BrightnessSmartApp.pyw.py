import tkinter as tk
from tkinter import ttk
import threading
import time
import cv2
import numpy as np
from PIL import Image, ImageDraw
import pystray
import ctypes
from ctypes import wintypes
import winreg

# --- WINDOWS HARDWARE LAYER (VCP/DDCCI) ---
user32 = ctypes.WinDLL('user32')
dxva2 = ctypes.WinDLL('dxva2')

class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [('hPhysicalMonitor', wintypes.HANDLE),
                ('szPhysicalMonitorDescription', wintypes.WCHAR * 128)]

def get_primary_monitor_handle():
    monitors_list = []
    def cb(hMonitor, hdcMonitor, lprcMonitor, dwData):
        count = wintypes.DWORD()
        if dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hMonitor, ctypes.byref(count)):
            p_array = (PHYSICAL_MONITOR * count.value)()
            if dxva2.GetPhysicalMonitorsFromHMONITOR(hMonitor, count.value, p_array):
                for i in range(count.value): monitors_list.append(hMonitor)
        return True
    user32.EnumDisplayMonitors(None, None, ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)(cb), 0)
    return monitors_list[0] if monitors_list else None

def get_monitor_model_name():
    try:
        path = r"SYSTEM\CurrentControlSet\Enum\DISPLAY"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as display_key:
            for i in range(winreg.QueryInfoKey(display_key)[0]):
                try:
                    sub_path = f"{path}\\{winreg.EnumKey(display_key, i)}"
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub_path) as sub_key:
                        for j in range(winreg.QueryInfoKey(sub_key)[0]):
                            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{sub_path}\\{winreg.EnumKey(sub_key, j)}") as desc_key:
                                friendly, _ = winreg.QueryValueEx(desc_key, "DeviceDesc")
                                name = friendly.split(';')[-1] if ';' in friendly else friendly
                                return name.split('(')[-1].split(')')[0].strip() if "Generic Monitor (" in name else name
                except: continue
    except: pass
    return "Primary Monitor"

def set_windows_native_brightness(hMonitor, brightness_percent):
    if not hMonitor: return
    count = wintypes.DWORD()
    if dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hMonitor, ctypes.byref(count)):
        mons = (PHYSICAL_MONITOR * count.value)()
        if dxva2.GetPhysicalMonitorsFromHMONITOR(hMonitor, count.value, mons):
            for i in range(count.value): 
                dxva2.SetMonitorBrightness(mons[i].hPhysicalMonitor, max(10, min(100, int(brightness_percent))))
            dxva2.DestroyPhysicalMonitors(count.value, mons)

# --- SAFE DEVICE ENUMERATION LAYER ---
def get_hardware_camera_list():
    """ Queries the Windows Management Instrumentation system to safely parse 
        actual hardware peripheral strings without arbitrary VTable pointers. """
    camera_names = []
    try:
        import wmi
        c = wmi.WMI()
        # Find all active hardware PNP entities falling under the Video or Imaging classes
        wmi_query = "SELECT * FROM Win32_PnPEntity WHERE PNPClass = 'Camera' OR PNPClass = 'Image' OR GUID = '{ca3e7ab9-b4c3-40e5-9126-a0b334164401}'"
        for device in c.query(wmi_query):
            if device.Name and "audio" not in device.Name.lower():
                camera_names.append(device.Name)
    except Exception as e:
        print(f"WMI Scan Registry Notice: {e}")
        
    devices = []
    # Loop over indices to map found names to system capture indices
    for idx, name in enumerate(camera_names):
        devices.append((idx, name))
        
    # Standard fallback if no system peripherals are detected
    return devices if devices else [(0, "Default USB Video Device")]

# --- MAIN ENGINE APPLICATION ---
class SmartBrightnessApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Desktop Smart Brightness")
        self.root.geometry("450x470")
        self.root.resizable(False, False)
        self.root.configure(bg="#F0F0F0")
        
        # Engine Control Flags
        self.running = False
        self.last_known_cam_val = 100
        self.updating_programmatically = False
        self.minimize_to_tray_var = tk.IntVar(value=1)
        self.camera_list_cache = []
        
        self.root.bind("<Unmap>", self.on_minimize_triggered)
        try: self.setup_system_tray()
        except: pass

        self.build_ui()
        self.refresh_cameras()
        
        hMon = get_primary_monitor_handle()
        if hMon: set_windows_native_brightness(hMon, 60)

    def build_ui(self):
        self.menu_bar = tk.Menu(self.root)
        self.root.config(menu=self.menu_bar)
        
        settings_menu = tk.Menu(self.menu_bar, tearoff=0)
        self.menu_bar.add_cascade(label="Settings", menu=settings_menu)
        settings_menu.add_command(label="Refresh Peripherals", command=self.refresh_cameras)
        settings_menu.add_separator()
        settings_menu.add_command(label="Exit Completely", command=self.exit_application)

        btn_frame = ttk.Frame(self.root, padding=10)
        btn_frame.pack(pady=(15, 0))
        self.start_btn = ttk.Button(btn_frame, text="Start Auto-Service", command=self.start_service)
        self.start_btn.grid(row=0, column=0, padx=5)
        self.stop_btn = ttk.Button(btn_frame, text="Stop Auto-Service", command=self.stop_service, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=5)

        # Dropdown Box
        ttk.Label(self.root, text="Select Active Capture Device:").pack(pady=(10, 0))
        self.camera_var = tk.StringVar()
        self.camera_dropdown = ttk.Combobox(self.root, textvariable=self.camera_var, state="readonly")
        self.camera_dropdown.pack(fill="x", padx=40, pady=(5, 0))
        
        # Floor Slider
        ttk.Label(self.root, text="Minimum Brightness Floor (%):").pack(pady=(12, 0))
        fl_f = ttk.Frame(self.root); fl_f.pack(fill="x", padx=40)
        self.floor_var = tk.IntVar(value=50)
        self.floor_scale = tk.Scale(fl_f, from_=10, to=100, orient="horizontal", variable=self.floor_var, showvalue=False, bd=0, highlightthickness=0, bg="#F0F0F0", troughcolor="#8E8E93", command=lambda v: self.sync() if self.running else None)
        self.floor_scale.pack(side="left", fill="x", expand=True, padx=(0, 10))
        tk.Entry(fl_f, textvariable=self.floor_var, width=4, justify="center", bd=1, relief="solid").pack(side="right")

        # Offset Slider
        ttk.Label(self.root, text="Calibration Offset (Fine Tuning):").pack(pady=(10, 0))
        off_f = ttk.Frame(self.root); off_f.pack(fill="x", padx=40)
        self.offset_var = tk.IntVar(value=0)
        self.offset_scale = tk.Scale(off_f, from_=-30, to=30, orient="horizontal", variable=self.offset_var, showvalue=False, bd=0, highlightthickness=0, bg="#F0F0F0", troughcolor="#8E8E93", command=lambda v: self.sync() if self.running else None)
        self.offset_scale.pack(side="left", fill="x", expand=True, padx=(0, 10))
        tk.Entry(off_f, textvariable=self.offset_var, width=4, justify="center", bd=1, relief="solid").pack(side="right")

        # Monitor Slider
        ttk.Label(self.root, text=f"{get_monitor_model_name()} Brightness Slider (%):", font=("Arial", 9, "bold")).pack(pady=(10, 0))
        m_f = ttk.Frame(self.root); m_f.pack(fill="x", padx=40)
        self.manual_var = tk.IntVar(value=60)
        self.manual_scale = tk.Scale(m_f, from_=10, to=100, orient="horizontal", variable=self.manual_var, showvalue=False, bd=0, highlightthickness=0, bg="#F0F0F0", troughcolor="#8E8E93", command=self.manual_scale_moved)
        self.manual_scale.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.manual_entry = tk.Entry(m_f, textvariable=self.manual_var, width=4, justify="center", bd=1, relief="solid")
        self.manual_entry.pack(side="right")

        # Tray Option Checkbox
        config_frame = ttk.LabelFrame(self.root, text=" Configuration Settings ", padding=12)
        config_frame.pack(fill="x", padx=40, pady=12)
        tray_check = ttk.Checkbutton(config_frame, text="Minimize to tray", variable=self.minimize_to_tray_var)
        tray_check.pack(anchor="w")

        self.status = ttk.Label(self.root, text="Status: Idle", font=("Arial", 10, "italic"))
        self.status.pack(pady=5)

    def refresh_cameras(self):
        self.camera_list_cache = get_hardware_camera_list()
        self.camera_dropdown['values'] = [dev[1] for dev in self.camera_list_cache]
        if self.camera_list_cache:
            self.camera_dropdown.current(0)
        else:
            self.camera_dropdown.set("No Peripheral Cameras Detected")

    def manual_scale_moved(self, val):
        if not self.updating_programmatically:
            if self.running: 
                self.stop_service()
                self.status.config(text="Status: Auto stopped due to manual shift.")
            set_windows_native_brightness(get_primary_monitor_handle(), val)

    def sync(self):
        floor, offset = self.floor_var.get(), self.offset_var.get()
        raw = floor + (self.last_known_cam_val - 20) * (100 - floor) / 160
        target = int(max(floor, min(100, np.clip(raw + offset, floor, 100))))
        
        set_windows_native_brightness(get_primary_monitor_handle(), target)
        self.status.config(text=f"Status: Live Auto-Set to {target}% (Room Light: {int(self.last_known_cam_val)})")
        
        self.updating_programmatically = True
        self.manual_var.set(target)
        self.updating_programmatically = False

    def start_service(self):
        self.running = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="enabled")
        self.camera_dropdown.config(state="disabled")
        self.manual_scale.config(state="disabled", troughcolor="#D1D1D6")
        self.manual_entry.config(state="disabled")
        threading.Thread(target=self.camera_loop, daemon=True).start()

    def stop_service(self):
        self.running = False
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.camera_dropdown.config(state="readonly")
        self.manual_scale.config(state="normal", troughcolor="#8E8E93")
        self.manual_entry.config(state="normal")
        self.status.config(text="Status: Stopped")

    def camera_loop(self):
        while self.running:
            cur_idx = self.camera_dropdown.current()
            target_hw_index = self.camera_list_cache[cur_idx][0] if cur_idx >= 0 else 0
            
            cap = cv2.VideoCapture(target_hw_index, cv2.CAP_DSHOW)
            if cap.isOpened():
                for _ in range(15): 
                    if not self.running: break
                    time.sleep(0.1)
                ret, frame = cap.read()
                cap.release()
                if ret and self.running:
                    self.last_known_cam_val = np.mean(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
                    self.root.after(0, self.sync)
                    
            for _ in range(300):
                if not self.running: break
                time.sleep(1)

    def on_minimize_triggered(self, event):
        if self.root.state() == "iconic" and self.minimize_to_tray_var.get() == 1:
            self.root.withdraw()

    def show_window(self):
        self.root.after(0, self.root.deiconify)
        self.root.after(10, self.root.state, "normal")

    def setup_system_tray(self):
        img = Image.new('RGB', (64, 64), (31, 119, 180))
        ImageDraw.Draw(img).rectangle([(16, 16), (48, 48)], fill=(255, 255, 255))
        
        tray_menu = pystray.Menu(
            pystray.MenuItem('Open Dashboard', self.show_window, default=True),
            pystray.MenuItem('Exit Completely', self.exit_application)
        )
        self.tray_icon = pystray.Icon("BrightnessCoreTray", img, "Smart Brightness Core", tray_menu)
        self.tray_icon.run_detached()

    def exit_application(self):
        self.running = False
        try: self.tray_icon.stop()
        except: pass
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = SmartBrightnessApp(root)
    root.protocol("WM_DELETE_WINDOW", app.exit_application)
    root.mainloop()