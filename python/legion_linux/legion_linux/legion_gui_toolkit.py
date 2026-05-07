#!/usr/bin/env python3
"""
Legion Linux Toolkit — Dashboard GUI  v0.6.3
New: LLL integration, IC temp, AC/battery auto-switching, kernel 7.x fallback.
KDE Plasma 6 / Wayland compatible.
"""

import os, sys, subprocess, json, time, threading
from pathlib import Path

try:
    from kernel_check import get_fan_status_message
except ImportError:
    def get_fan_status_message(): return ""

# Make legion_linux importable when running from source
_this_dir = Path(__file__).resolve().parent
_src_root = _this_dir.parent.parent
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))

os.environ["QT_QPA_PLATFORM"] = "wayland"
os.environ["QT_WAYLAND_DISABLE_WINDOWDECORATION"] = "1"
os.environ.setdefault("WAYLAND_DISPLAY", "wayland-0")
if "XDG_RUNTIME_DIR" not in os.environ:
    os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"

# ── Legion logo icon ────────────────────────────────────────────────────────
_ICON_DIR = Path(__file__).resolve().parent
_LEGION_ICON_PATH = _ICON_DIR / "legion_logo_toolkit.png"

def _legion_icon():
    from PyQt6.QtGui import QIcon as _ic, QPixmap as _px
    pm = _px(str(_LEGION_ICON_PATH))
    return _ic(pm) if not pm.isNull() else _ic()



from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFrame, QScrollArea, QSizePolicy,
    QSlider, QStackedWidget, QComboBox, QToolTip, QSpinBox,
    QDoubleSpinBox, QLineEdit
)
from PyQt6.QtCore import (Qt, QTimer, QPropertyAnimation, QEasingCurve,
                           pyqtProperty, QThread, pyqtSignal, QPoint)
from PyQt6.QtGui import QColor, QPainter, QPen, QBrush, QFont, QCursor

# ══════════════════════════════════════════════════════════════════════════════
# PATHS
# ══════════════════════════════════════════════════════════════════════════════
PLATFORM_PROFILE  = Path("/sys/firmware/acpi/platform_profile")
_POWERMODE_MAP = {1: "quiet", 2: "balanced", 3: "performance", 255: "custom"}

LEGION_SYS_BASEPATH = Path("/sys/module/legion_laptop/drivers/platform:legion/legion")
LEGION_POWERMODE    = LEGION_SYS_BASEPATH / "powermode"

def _read_powermode() -> str:
    try:
        return _POWERMODE_MAP.get(int(LEGION_POWERMODE.read_text().strip()), "balanced")
    except:
        return "balanced"
AMD_BOOST         = Path("/sys/devices/system/cpu/cpufreq/boost")
DAEMON_BIN        = Path("/usr/lib/legion-toolkit/legion-daemon.py")

# ══════════════════════════════════════════════════════════════════════════════
# DYNAMIC SYSFS PATH DETECTION
# Works for ALL Lenovo models across all brands and generations.
# Never hardcodes a specific path — scans the actual filesystem at startup.
# ══════════════════════════════════════════════════════════════════════════════

def _find_feature(feature: str) -> "Path | None":
    """
    Dynamically find a Lenovo feature file anywhere in sysfs.
    Searches ideapad_acpi, legion_laptop, wmi, platform and devices trees.
    """
    # Ordered list of search bases — checked in priority order
    search_bases = []

    # 1. ideapad_acpi — IdeaPad, Legion, Yoga, LOQ, ThinkBook
    ideapad_root = Path("/sys/bus/platform/drivers/ideapad_acpi")
    if ideapad_root.exists():
        try:
            for d in ideapad_root.iterdir():
                if d.is_dir() or d.is_symlink():
                    search_bases.append(d)
        except: pass

    # 2. legion_laptop driver — Legion, LOQ (any generation, any PCI slot)
    for legion_root in [
        Path("/sys/bus/platform/drivers/legion"),
        Path("/sys/module/legion_laptop/drivers/platform:legion"),
    ]:
        if legion_root.exists():
            try:
                for d in legion_root.iterdir():
                    search_bases.append(d)
            except: pass

    # 3. PNP0C09/VPC2004 device — scan all PCI buses (different slot on different models, kernel 7.x uses VPC2004)
    try:
        for pci in Path("/sys/devices").glob("pci*"):
            for dev in pci.glob("*/PNP0C09:*"):
                search_bases.append(dev)
            for dev in pci.glob("*/VPC2004:*"):
                search_bases.append(dev)
    except: pass

    # 4. WMI devices — some features exposed via WMI
    wmi_root = Path("/sys/bus/wmi/devices")
    if wmi_root.exists():
        try:
            for d in wmi_root.iterdir():
                search_bases.append(d)
        except: pass

    # 5. General platform devices
    plat_root = Path("/sys/bus/platform/devices")
    if plat_root.exists():
        try:
            for d in plat_root.iterdir():
                n = d.name.lower()
                if any(k in n for k in ["vpc","ideapad","legion","lenovo","thinkpad"]):
                    search_bases.append(d)
        except: pass

    # Search all bases for the feature file
    for base in search_bases:
        try:
            p = Path(base) / feature
            if p.exists(): return p
        except: pass

    return None


def _find_ideapad(feature: str) -> "Path | None":
    """Find ideapad_acpi specific feature (conservation_mode, fn_lock etc.)"""
    root = Path("/sys/bus/platform/drivers/ideapad_acpi")
    if root.exists():
        try:
            for d in root.iterdir():
                p = d / feature
                if p.exists(): return p
        except: pass
    # Fallback to general scan
    return _find_feature(feature)


# ── Resolve all feature paths at startup ─────────────────────────────────────
IDEAPAD_BASE      = (lambda: next(
    (d for d in Path("/sys/bus/platform/drivers/ideapad_acpi").iterdir()
     if (d / "conservation_mode").exists() or (d / "fn_lock").exists()),
    Path("/sys/bus/platform/drivers/ideapad_acpi/VPC2004:00")
) if Path("/sys/bus/platform/drivers/ideapad_acpi").exists()
  else Path("/sys/bus/platform/drivers/ideapad_acpi/VPC2004:00"))()

LEGION_SYS_BASEPATH = LEGION_SYS_BASEPATH  # from line 47

# G-Sync — use the LLL device path
_GSYNC_PATH  = LEGION_SYS_BASEPATH / "gsync"

CONSERVATION_MODE = _find_ideapad("conservation_mode") or IDEAPAD_BASE / "conservation_mode"
CAMERA_POWER      = _find_ideapad("camera_power")      or IDEAPAD_BASE / "camera_power"
FN_LOCK           = _find_ideapad("fn_lock")            or IDEAPAD_BASE / "fn_lock"
USB_CHARGING      = _find_ideapad("usb_charging")       or IDEAPAD_BASE / "usb_charging"

TOUCHPAD          = LEGION_SYS_BASEPATH / "touchpad"
RAPID_CHARGE      = LEGION_SYS_BASEPATH / "rapidcharge"
WINKEY            = LEGION_SYS_BASEPATH / "winkey"
OVERDRIVE         = LEGION_SYS_BASEPATH / "overdrive"
GSYNC             = _GSYNC_PATH
NVIDIA_BACKLIGHT = Path("/sys/class/backlight/nvidia_wmi_ec_backlight/brightness")
POWER_CHARGE_MODE = LEGION_SYS_BASEPATH / "powerchargemode"
THERMAL_MODE      = LEGION_SYS_BASEPATH / "thermalmode"
FAN_FULLSPEED     = LEGION_SYS_BASEPATH / "fan_fullspeed"

LEGION_BASE = LEGION_SYS_BASEPATH

def get_gsync_status() -> bool:
    """Check if G-Sync (hybrid mode) is enabled."""
    if _GSYNC_PATH.exists():
        try:
            return _GSYNC_PATH.read_text().strip() == "1"
        except:
            pass
    return False

def set_gsync(enable: bool) -> tuple[bool, str]:
    """Enable/disable G-Sync."""
    if _GSYNC_PATH.exists():
        try:
            subprocess.run(["pkexec", "sh", "-c", f"echo {'1' if enable else '0'} > {_GSYNC_PATH}"],
                        capture_output=True, timeout=5)
            return True, f"G-Sync {'enabled' if enable else 'disabled'}"
        except Exception as e:
            return False, str(e)[:80]
    return False, "G-Sync not available on this system"

def get_gpu_hybrid_status() -> bool:
    """Check if GPU is in hybrid mode (switchable graphics)."""
    try:
        r = subprocess.run(["which", "envycontrol"], capture_output=True)
        if r.returncode != 0:
            return False
        r = subprocess.run(["envycontrol", "query"], capture_output=True, text=True)
        return "hybrid" in r.stdout.lower()
    except:
        return False

# ── Flip to Start / Instant Boot ─────────────────────────────────────────────
def _find_sysfs_feature(names: list) -> "Path | None":
    for name in names:
        p = _find_feature(name)
        if p: return p
    return None

FLIP_TO_START = _find_sysfs_feature(["flip_to_start","fliptostart","flip_to_boot","fliptoboot"])
INSTANT_BOOT  = _find_sysfs_feature(["instant_boot","instantboot","instant_on","ac_boot"])
BAT               = Path("/sys/class/power_supply/BAT0")
ACTIONS_CFG       = Path.home() / ".config/legion-toolkit/actions.json"
OC_CFG            = Path.home() / ".config/legion-toolkit/overclock.json"
CFG_DIR         = Path.home() / ".config/legion-toolkit"
FAN_CFG           = Path.home() / ".config/legion-toolkit/fan.json"
APP_CFG           = Path.home() / ".config/legion-toolkit/appearance.json"
HARDWARE_CFG      = Path.home() / ".config/legion-toolkit/hardware.json"
LANG_CFG          = Path.home() / ".config/legion-toolkit/language.json"
FIRST_RUN_FLAG    = Path.home() / ".config/legion-toolkit/first_run_done"

# ══════════════════════════════════════════════════════════════════════════════
# TRANSLATIONS
# ══════════════════════════════════════════════════════════════════════════════
_LANG = "en"   # set by first-run wizard or saved config

_TR = {
    "en": {
        "app_name":"Legion Linux Toolkit","home":"Home","battery":"Battery",
        "performance":"Performance","display":"Display","keyboard":"Keyboard",
        "system":"System","overclock":"Overclock","fan":"Fan","actions":"Actions",
        "about":"About","power_mode":"Power Mode","battery_mode":"Battery Mode",
        "gpu_mode":"GPU Working Mode","apply":"Apply","save":"Save",
        "enabled":"Enabled","disabled":"Disabled","on":"ON","off":"OFF",
        "auto":"Auto","full_speed":"Full Speed","detecting":"Detecting hardware…",
        "welcome":"Welcome to Legion Linux Toolkit",
        "choose_lang":"Choose your language to get started.",
        "hw_detect_title":"Hardware Detection",
        "hw_detect_desc":"Scanning your device — this runs once and is saved.",
        "hw_done":"Detection complete!","next":"Next","finish":"Finish",
        "brightness":"Brightness","resolution":"Resolution",
        "refresh_rate":"Refresh Rate","theme":"Theme",
        "conservation":"Conservation (~60%)","rapid_charge":"Rapid Charge",
        "normal":"Normal","quiet":"Quiet","balanced":"Balanced",
        "performance_label":"Performance","custom":"Custom",
    },
    "fr": {
        "app_name":"Legion Linux Toolkit","home":"Accueil","battery":"Batterie",
        "performance":"Performance","display":"Affichage","keyboard":"Clavier",
        "system":"Système","overclock":"Overclocking","fan":"Ventilateur",
        "actions":"Actions","about":"À propos","power_mode":"Mode d'alimentation",
        "battery_mode":"Mode batterie","gpu_mode":"Mode GPU",
        "apply":"Appliquer","save":"Enregistrer",
        "enabled":"Activé","disabled":"Désactivé","on":"OUI","off":"NON",
        "auto":"Auto","full_speed":"Vitesse max","detecting":"Détection…",
        "welcome":"Bienvenue dans Legion Linux Toolkit",
        "choose_lang":"Choisissez votre langue.",
        "hw_detect_title":"Détection matérielle",
        "hw_detect_desc":"Analyse de votre appareil — exécuté une seule fois.",
        "hw_done":"Détection terminée !","next":"Suivant","finish":"Terminer",
        "brightness":"Luminosité","resolution":"Résolution",
        "refresh_rate":"Taux de rafraîchissement","theme":"Thème",
        "conservation":"Conservation (~60%)","rapid_charge":"Charge rapide",
        "normal":"Normal","quiet":"Silencieux","balanced":"Équilibré",
        "performance_label":"Performance","custom":"Personnalisé",
    },
    "de": {
        "app_name":"Legion Linux Toolkit","home":"Start","battery":"Akku",
        "performance":"Leistung","display":"Anzeige","keyboard":"Tastatur",
        "system":"System","overclock":"Übertaktung","fan":"Lüfter",
        "actions":"Aktionen","about":"Über","power_mode":"Energiemodus",
        "battery_mode":"Akkumodus","gpu_mode":"GPU-Modus",
        "apply":"Anwenden","save":"Speichern",
        "enabled":"Aktiviert","disabled":"Deaktiviert","on":"AN","off":"AUS",
        "auto":"Auto","full_speed":"Volle Drehzahl","detecting":"Erkennung…",
        "welcome":"Willkommen bei Legion Linux Toolkit",
        "choose_lang":"Wählen Sie Ihre Sprache.",
        "hw_detect_title":"Hardware-Erkennung",
        "hw_detect_desc":"Gerät wird einmalig gescannt und gespeichert.",
        "hw_done":"Erkennung abgeschlossen!","next":"Weiter","finish":"Fertig",
        "brightness":"Helligkeit","resolution":"Auflösung",
        "refresh_rate":"Bildwiederholrate","theme":"Design",
        "conservation":"Schutz (~60%)","rapid_charge":"Schnellladen",
        "normal":"Normal","quiet":"Leise","balanced":"Ausgewogen",
        "performance_label":"Leistung","custom":"Benutzerdefiniert",
    },
    "es": {
        "app_name":"Legion Linux Toolkit","home":"Inicio","battery":"Batería",
        "performance":"Rendimiento","display":"Pantalla","keyboard":"Teclado",
        "system":"Sistema","overclock":"Overclocking","fan":"Ventilador",
        "actions":"Acciones","about":"Acerca de","power_mode":"Modo energía",
        "battery_mode":"Modo batería","gpu_mode":"Modo GPU",
        "apply":"Aplicar","save":"Guardar",
        "enabled":"Activado","disabled":"Desactivado","on":"ON","off":"OFF",
        "auto":"Auto","full_speed":"Velocidad máx","detecting":"Detectando…",
        "welcome":"Bienvenido a Legion Linux Toolkit",
        "choose_lang":"Elige tu idioma.",
        "hw_detect_title":"Detección de hardware",
        "hw_detect_desc":"Escaneando tu dispositivo — se ejecuta una vez.",
        "hw_done":"¡Detección completa!","next":"Siguiente","finish":"Finalizar",
        "brightness":"Brillo","resolution":"Resolución",
        "refresh_rate":"Tasa de refresco","theme":"Tema",
        "conservation":"Conservación (~60%)","rapid_charge":"Carga rápida",
        "normal":"Normal","quiet":"Silencioso","balanced":"Equilibrado",
        "performance_label":"Rendimiento","custom":"Personalizado",
    },
    "pt": {
        "app_name":"Legion Linux Toolkit","home":"Início","battery":"Bateria",
        "performance":"Desempenho","display":"Ecrã","keyboard":"Teclado",
        "system":"Sistema","overclock":"Overclocking","fan":"Ventoinha",
        "actions":"Ações","about":"Sobre","power_mode":"Modo de energia",
        "battery_mode":"Modo bateria","gpu_mode":"Modo GPU",
        "apply":"Aplicar","save":"Guardar",
        "enabled":"Ativado","disabled":"Desativado","on":"ON","off":"OFF",
        "auto":"Auto","full_speed":"Vel. máxima","detecting":"A detetar…",
        "welcome":"Bem-vindo ao Legion Linux Toolkit",
        "choose_lang":"Escolha o seu idioma.",
        "hw_detect_title":"Deteção de hardware",
        "hw_detect_desc":"A analisar o seu dispositivo — executado uma vez.",
        "hw_done":"Deteção concluída!","next":"Seguinte","finish":"Concluir",
        "brightness":"Brilho","resolution":"Resolução",
        "refresh_rate":"Taxa de atualização","theme":"Tema",
        "conservation":"Conservação (~60%)","rapid_charge":"Carga rápida",
        "normal":"Normal","quiet":"Silencioso","balanced":"Equilibrado",
        "performance_label":"Desempenho","custom":"Personalizado",
    },
    "tr": {
        "app_name":"Legion Linux Toolkit","home":"Ana Sayfa","battery":"Pil",
        "performance":"Performans","display":"Ekran","keyboard":"Klavye",
        "system":"Sistem","overclock":"Hız Aşırtma","fan":"Fan",
        "actions":"Eylemler","about":"Hakkında","power_mode":"Güç modu",
        "battery_mode":"Pil modu","gpu_mode":"GPU modu",
        "apply":"Uygula","save":"Kaydet",
        "enabled":"Etkin","disabled":"Devre dışı","on":"AÇIK","off":"KAPALI",
        "auto":"Otomatik","full_speed":"Tam hız","detecting":"Algılanıyor…",
        "welcome":"Legion Linux Toolkit'e Hoş Geldiniz",
        "choose_lang":"Dilinizi seçin.",
        "hw_detect_title":"Donanım Algılama",
        "hw_detect_desc":"Cihazınız taranıyor — yalnızca bir kez çalışır.",
        "hw_done":"Algılama tamamlandı!","next":"İleri","finish":"Bitir",
        "brightness":"Parlaklık","resolution":"Çözünürlük",
        "refresh_rate":"Yenileme hızı","theme":"Tema",
        "conservation":"Koruma (~%60)","rapid_charge":"Hızlı şarj",
        "normal":"Normal","quiet":"Sessiz","balanced":"Dengeli",
        "performance_label":"Performans","custom":"Özel",
    },
    "ru": {
        "app_name":"Legion Linux Toolkit","home":"Главная","battery":"Батарея",
        "performance":"Производительность","display":"Дисплей","keyboard":"Клавиатура",
        "system":"Система","overclock":"Разгон","fan":"Вентилятор",
        "actions":"Действия","about":"О программе","power_mode":"Режим питания",
        "battery_mode":"Режим батареи","gpu_mode":"Режим GPU",
        "apply":"Применить","save":"Сохранить",
        "enabled":"Включено","disabled":"Выключено","on":"ВКЛ","off":"ВЫКЛ",
        "auto":"Авто","full_speed":"Макс. скорость","detecting":"Определение…",
        "welcome":"Добро пожаловать в Legion Linux Toolkit",
        "choose_lang":"Выберите язык.",
        "hw_detect_title":"Обнаружение оборудования",
        "hw_detect_desc":"Сканирование устройства — выполняется один раз.",
        "hw_done":"Обнаружение завершено!","next":"Далее","finish":"Готово",
        "brightness":"Яркость","resolution":"Разрешение",
        "refresh_rate":"Частота обновления","theme":"Тема",
        "conservation":"Защита (~60%)","rapid_charge":"Быстрая зарядка",
        "normal":"Нормальный","quiet":"Тихий","balanced":"Сбалансированный",
        "performance_label":"Производительность","custom":"Пользовательский",
    },
    "zh": {
        "app_name":"军团 Linux 工具包","home":"主页","battery":"电池",
        "performance":"性能","display":"显示","keyboard":"键盘",
        "system":"系统","overclock":"超频","fan":"风扇",
        "actions":"操作","about":"关于","power_mode":"电源模式",
        "battery_mode":"电池模式","gpu_mode":"GPU 模式",
        "apply":"应用","save":"保存",
        "enabled":"已启用","disabled":"已禁用","on":"开","off":"关",
        "auto":"自动","full_speed":"全速","detecting":"检测中…",
        "welcome":"欢迎使用军团 Linux 工具包",
        "choose_lang":"请选择您的语言。",
        "hw_detect_title":"硬件检测",
        "hw_detect_desc":"正在扫描您的设备 — 仅运行一次。",
        "hw_done":"检测完成！","next":"下一步","finish":"完成",
        "brightness":"亮度","resolution":"分辨率",
        "refresh_rate":"刷新率","theme":"主题",
        "conservation":"保护模式 (~60%)","rapid_charge":"快速充电",
        "normal":"正常","quiet":"安静","balanced":"均衡",
        "performance_label":"性能","custom":"自定义",
    },
    "ja": {
        "app_name":"Legion Linux ツールキット","home":"ホーム","battery":"バッテリー",
        "performance":"パフォーマンス","display":"ディスプレイ","keyboard":"キーボード",
        "system":"システム","overclock":"オーバークロック","fan":"ファン",
        "actions":"アクション","about":"このアプリについて","power_mode":"電源モード",
        "battery_mode":"バッテリーモード","gpu_mode":"GPU モード",
        "apply":"適用","save":"保存",
        "enabled":"有効","disabled":"無効","on":"オン","off":"オフ",
        "auto":"自動","full_speed":"最大速度","detecting":"検出中…",
        "welcome":"Legion Linux ツールキットへようこそ",
        "choose_lang":"言語を選択してください。",
        "hw_detect_title":"ハードウェア検出",
        "hw_detect_desc":"デバイスをスキャン中 — 一度だけ実行されます。",
        "hw_done":"検出完了！","next":"次へ","finish":"完了",
        "brightness":"輝度","resolution":"解像度",
        "refresh_rate":"リフレッシュレート","theme":"テーマ",
        "conservation":"保護モード (~60%)","rapid_charge":"急速充電",
        "normal":"通常","quiet":"静音","balanced":"バランス",
        "performance_label":"パフォーマンス","custom":"カスタム",
    },
    "ko": {
        "app_name":"Legion Linux 툴킷","home":"홈","battery":"배터리",
        "performance":"성능","display":"디스플레이","keyboard":"키보드",
        "system":"시스템","overclock":"오버클럭","fan":"팬",
        "actions":"작업","about":"정보","power_mode":"전원 모드",
        "battery_mode":"배터리 모드","gpu_mode":"GPU 모드",
        "apply":"적용","save":"저장",
        "enabled":"활성화","disabled":"비활성화","on":"켜짐","off":"꺼짐",
        "auto":"자동","full_speed":"최대 속도","detecting":"감지 중…",
        "welcome":"Legion Linux 툴킷에 오신 것을 환영합니다",
        "choose_lang":"언어를 선택하세요.",
        "hw_detect_title":"하드웨어 감지",
        "hw_detect_desc":"장치 스캔 중 — 한 번만 실행됩니다.",
        "hw_done":"감지 완료!","next":"다음","finish":"완료",
        "brightness":"밝기","resolution":"해상도",
        "refresh_rate":"주사율","theme":"테마",
        "conservation":"보호 모드 (~60%)","rapid_charge":"급속 충전",
        "normal":"일반","quiet":"조용함","balanced":"균형",
        "performance_label":"성능","custom":"사용자 지정",
    },
    "ar": {
        "app_name":"Legion Linux Toolkit","home":"الرئيسية","battery":"البطارية",
        "performance":"الأداء","display":"الشاشة","keyboard":"لوحة المفاتيح",
        "system":"النظام","overclock":"رفع التردد","fan":"المروحة",
        "actions":"الإجراءات","about":"حول","power_mode":"وضع الطاقة",
        "battery_mode":"وضع البطارية","gpu_mode":"وضع GPU",
        "apply":"تطبيق","save":"حفظ",
        "enabled":"مُفعَّل","disabled":"معطَّل","on":"تشغيل","off":"إيقاف",
        "auto":"تلقائي","full_speed":"السرعة الكاملة","detecting":"جارٍ الاكتشاف…",
        "welcome":"مرحباً بك في Legion Linux Toolkit",
        "choose_lang":"اختر لغتك للبدء.",
        "hw_detect_title":"اكتشاف الأجهزة",
        "hw_detect_desc":"جارٍ مسح الجهاز — يعمل مرة واحدة فقط.",
        "hw_done":"اكتمل الاكتشاف!","next":"التالي","finish":"إنهاء",
        "brightness":"السطوع","resolution":"الدقة",
        "refresh_rate":"معدل التحديث","theme":"السمة",
        "conservation":"الحماية (~60%)","rapid_charge":"الشحن السريع",
        "normal":"عادي","quiet":"صامت","balanced":"متوازن",
        "performance_label":"أداء","custom":"مخصص",
    },
}

_LANG_NAMES = {
    "en":"English","fr":"Français","de":"Deutsch","es":"Español",
    "pt":"Português","tr":"Türkçe","ru":"Русский","zh":"中文",
    "ja":"日本語","ko":"한국어","ar":"العربية",
}

def tr(key: str) -> str:
    """Translate a key using the current language, fall back to English."""
    return _TR.get(_LANG, _TR["en"]).get(key, _TR["en"].get(key, key))

def load_language():
    global _LANG
    try:
        if LANG_CFG.exists():
            _LANG = json.loads(LANG_CFG.read_text()).get("lang","en")
    except: _LANG = "en"

def save_language(lang: str):
    global _LANG
    _LANG = lang
    try:
        LANG_CFG.parent.mkdir(parents=True, exist_ok=True)
        LANG_CFG.write_text(json.dumps({"lang": lang}))
    except: pass

# ══════════════════════════════════════════════════════════════════════════════
# HARDWARE DETECTION
# ══════════════════════════════════════════════════════════════════════════════
HARDWARE_CACHE_TTL = 3600  # Cache for 1 hour

def _dmi(field: str) -> str:
    try: return Path(f"/sys/class/dmi/id/{field}").read_text().strip().lower()
    except: return ""

def _read_file(path: str, default: str = "") -> str:
    """Safely read a file, return default on error."""
    try: return Path(path).read_text().strip()
    except: return default

def _which(cmd: str) -> bool:
    """Check if command exists in PATH."""
    return Path(cmd).exists() or subprocess.run(["which", cmd], capture_output=True).returncode == 0

def _exists_quiet(paths: list) -> bool:
    """Check if any path exists."""
    return any(Path(p).exists() for p in paths)

# Legion model mapping for better detection
LEGION_MODELS = {
    "82ju": "Legion 5 15ACH6H",
    "82gu": "Legion 5 15ACH5",
    "82ms": "Legion 7 16ACHg6",
    "82rh": "Legion 5 Pro 16ARH7",
    "82sr": "Legion 5 Pro 16",
    "82ts": "Legion 7 16",
    "82wm": "Legion Slim 7",
}

def detect_hardware(force: bool = False) -> dict:
    """
    Detect Lenovo brand, model and hardware capabilities.
    Add force=True to bypass cache and re-detect.
    """
    # Check cache first
    if not force:
        cached = load_hardware()
        if cached:
            import time
            cached_time = cached.get("_detected_at", 0)
            if time.time() - cached_time < HARDWARE_CACHE_TTL:
                return cached

    vendor      = _dmi("sys_vendor")
    product     = _dmi("product_name")
    family      = _dmi("product_family")
    chassis     = _dmi("chassis_type")

    # ── Enhanced brand detection ──────────────────────────────────────────
    full = f"{product} {family}".lower()
    
    # Legion detection with model codes
    if "legion" in full:
        brand = "legion"
        # Try to get detailed model name
        product_code = product[:4] if product else ""
        if product_code in LEGION_MODELS:
            model_detail = LEGION_MODELS[product_code]
        else:
            # Try to extract model from product name
            import re
            match = re.search(r'(legion\s+\d+|loq\s+\d+)', full, re.IGNORECASE)
            model_detail = match.group(0).title() if match else product.title()
    elif "loq" in full:
        brand = "loq"
        model_detail = product.title() if product else "LOQ"
    elif "thinkpad" in full:
        brand = "thinkpad"
        model_detail = product.title() if product else "ThinkPad"
    elif "thinkbook" in full:
        brand = "thinkbook"
        model_detail = product.title() if product else "ThinkBook"
    elif "yoga" in full:
        brand = "yoga"
        model_detail = product.title() if product else "Yoga"
    elif any(k in full for k in ["ideapad", "idea pad", "flex", "slim"]):
        brand = "ideapad"
        model_detail = product.title() if product else "IdeaPad"
    else:
        brand = "lenovo" if "lenovo" in vendor else "unknown"
        model_detail = product.title() if product else "Unknown"

    # ── CPU vendor detection ──────────────────────────────────────────────────
    cpu_vendor = "unknown"
    cpu_name   = "Unknown"
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if "vendor_id" in line.lower():
                v = line.split(":")[1].strip().lower()
                if "amd" in v:   cpu_vendor = "amd"
                elif "intel" in v: cpu_vendor = "intel"
            if "model name" in line.lower() and cpu_name == "Unknown":
                cpu_name = line.split(":")[1].strip()
    except: pass

    # ── GPU detection — Optimized ───────────────────────────────────────────
    has_nvidia = False
    has_amd_gpu = False
    has_intel_gpu = False
    
    # Try lspci first (most reliable)
    try:
        lspci = subprocess.run(["lspci"], capture_output=True, text=True, timeout=3).stdout.lower()
        has_nvidia = "nvidia" in lspci
        has_amd_gpu = any(k in lspci for k in ["amd", "radeon", "amdgpu"])
        has_intel_gpu = any(k in lspci for k in ["intel", "arc", "xe"])
    except:
        # Fallback: check sysfs
        nvidia_sysfs = Path("/sys/bus/pci/drivers/nvidia")
        has_nvidia = nvidia_sysfs.exists()
        
        amd_gpu_sysfs = Path("/sys/class/drm")
        if amd_gpu_sysfs.exists():
            for card in amd_gpu_sysfs.glob("card*/device/vendor"):
                try:
                    vendor = card.read_text().strip().lower()
                    if "1002" in vendor:  # AMD vendor ID
                        has_amd_gpu = True
                    elif "8086" in vendor:  # Intel vendor ID
                        has_intel_gpu = True
                except:
                    pass

    # ── Intel-specific paths ─────────────────────────────────────────────────
    # Intel TurboBoost
    intel_boost_path = Path("/sys/devices/system/cpu/intel_pstate/no_turbo")
    # Intel powercap RAPL
    intel_rapl = any(Path("/sys/class/powercap").glob("intel-rapl:*")) \
                 if Path("/sys/class/powercap").exists() else False
    # Intel GPU sysfs
    intel_gpu_sysfs = bool(list(Path("/sys/class/drm").glob("card*/device/vendor"))
                           if Path("/sys/class/drm").exists() else [])

    # ── Fingerprint — multiple drivers ───────────────────────────────────────
    fp_drivers = [
        "/sys/bus/usb/drivers/validity-sensor",
        "/sys/bus/usb/drivers/synaptics-usb",
        "/sys/bus/usb/drivers/fpc_fingerprint",
        "/sys/bus/usb/drivers/elan-fingerprint",
        "/sys/bus/platform/drivers/fingerprint",
    ]
    has_fingerprint = any(Path(d).exists() and list(Path(d).glob("*"))
                          for d in fp_drivers)

    def ex(p): return Path(p).exists()

    cap = {
        # Identity
        "brand":      brand,
        "model":      _dmi("product_name"),
        "vendor":     _dmi("sys_vendor"),
        "family":     _dmi("product_family"),
        "cpu_vendor": cpu_vendor,
        "cpu_name":   cpu_name,
        "has_nvidia":    has_nvidia,
        "has_amd_gpu":   has_amd_gpu,
        "has_intel_gpu": has_intel_gpu,

        # Power
        "platform_profile":  ex("/sys/firmware/acpi/platform_profile"),
        "conservation_mode": CONSERVATION_MODE.exists(),
        "rapidcharge":       RAPID_CHARGE.exists(),
        "powerchargemode":   POWER_CHARGE_MODE.exists(),

        # CPU boost — AMD or Intel
        "amd_boost":         AMD_BOOST.exists(),
        "intel_boost":       intel_boost_path.exists(),
        "intel_rapl":        intel_rapl,

        # Display
        "overdrive":  OVERDRIVE.exists(),
        "gsync":      GSYNC.exists(),
        "nw_backlight": NVIDIA_BACKLIGHT.exists(),

        # Input
        "fn_lock":      FN_LOCK.exists(),
        "camera":       CAMERA_POWER.exists(),
        "touchpad":     TOUCHPAD.exists(),
        "winkey":       WINKEY.exists(),
        "usb_charging": USB_CHARGING.exists(),

        # Fan
        "fan_fullspeed": FAN_FULLSPEED.exists(),
        "thermalmode":   THERMAL_MODE.exists(),
        "lockfancontroller": ex(LEGION_SYS_BASEPATH / "lockfancontroller"),
        "minifancurve":    ex(LEGION_SYS_BASEPATH / "minifancurve"),

        # Backlight
        "kbd_backlight":    ex("/sys/class/leds/platform::kbd_backlight/brightness"),
        "ylogo":         ex("/sys/class/leds/platform::ylogo/brightness"),
        "ioport":        ex("/sys/class/leds/platform::ioport/brightness"),
        "screen_backlight": bool(list(Path("/sys/class/backlight").iterdir())
                                 if Path("/sys/class/backlight").exists() else []),

        # ThinkPad-specific
        "tp_charge_start": ex("/sys/class/power_supply/BAT0/charge_start_threshold"),
        "tp_charge_stop":  ex("/sys/class/power_supply/BAT0/charge_stop_threshold"),
        "tp_fan_control":  ex("/proc/acpi/ibm/fan"),
        "tp_trackpoint":   bool(list(Path("/sys/bus/serio/devices").glob("*/speed"))
                                if Path("/sys/bus/serio/devices").exists() else []),
        "tp_thinklight":   ex("/sys/class/leds/tpacpi::thinklight/brightness"),
        "tp_micmute_led":  ex("/sys/class/leds/platform::micmute/brightness"),

        # Yoga-specific
        "yoga_hinge": ex("/sys/bus/platform/drivers/lenovo-ymc"),
        "als_sensor": bool(list(Path("/sys/bus/iio/devices").glob("*/in_illuminance_raw"))
                           if Path("/sys/bus/iio/devices").exists() else []),

        # Tools (cached check)
        "legionaura": _which("legionaura"),
        "envycontrol": _which("envycontrol"),

        # Misc
        "fingerprint": has_fingerprint,
        "wwan": bool(list(Path("/sys/class/net").glob("ww*"))
                    if Path("/sys/class/net").exists() else []),
    }
    return cap

def load_hardware() -> dict:
    try:
        if HARDWARE_CFG.exists():
            return json.loads(HARDWARE_CFG.read_text())
    except: pass
    return {}

def save_hardware(cap: dict):
    import time
    try:
        cap["_detected_at"] = int(time.time())
        HARDWARE_CFG.parent.mkdir(parents=True, exist_ok=True)
        HARDWARE_CFG.write_text(json.dumps(cap, indent=2))
    except: pass

# Global hardware profile — loaded at startup
HW: dict = {}

# L1 AI Engine — try multiple known paths from LenovoLegionLinux driver
_AI_ENGINE_PATHS = [
    Path("/sys/bus/platform/drivers/ideapad_acpi/VPC2004:00/ai_mode"),
    Path("/sys/bus/platform/devices/VPC2004:00/ai_mode"),
    Path("/sys/bus/wmi/drivers/lenovo-wmi-gamezone/ai_mode"),
]
AI_ENGINE = next((p for p in _AI_ENGINE_PATHS if p.exists()), None)

# RGB keyboard — LenovoLegionLinux driver paths
_KBD_BACKLIGHT_PATHS = [
    Path("/sys/class/leds/platform::kbd_backlight/brightness"),
    Path("/sys/class/leds/legion::kbd_backlight/brightness"),
]
KBD_BACKLIGHT_PATH = next((p for p in _KBD_BACKLIGHT_PATHS if p.exists()), None)
KBD_BACKLIGHT_MAX_PATH = None
if KBD_BACKLIGHT_PATH:
    KBD_BACKLIGHT_MAX_PATH = KBD_BACKLIGHT_PATH.parent / "max_brightness"

RGB_PRESETS = {
    # ── Static colours ───────────────────────────────────────────────────────
    "Static Red":      ("ff0000","ff0000","ff0000","ff0000"),
    "Static Blue":     ("0044ff","0044ff","0044ff","0044ff"),
    "Static Green":    ("00ff44","00ff44","00ff44","00ff44"),
    "Static White":    ("ffffff","ffffff","ffffff","ffffff"),
    "Static Purple":   ("aa00ff","aa00ff","aa00ff","aa00ff"),
    "Static Cyan":     ("00ffff","00ffff","00ffff","00ffff"),
    "Static Orange":   ("ff6600","ff6600","ff6600","ff6600"),
    "Static Pink":     ("ff69b4","ff69b4","ff69b4","ff69b4"),
    # ── Legion themed ────────────────────────────────────────────────────────
    "Legion Red":      ("cc0000","dd1111","ff0000","cc0000"),
    "Legion Blue":     ("001aff","0033ff","004cff","0033ff"),
    "Legion Storm":    ("0033ff","00aaff","00ffcc","00ff88"),
    # ── Gradients ────────────────────────────────────────────────────────────
    "Ocean":           ("001aff","0066ff","00aaff","00ffff"),
    "Sunset":          ("ff2200","ff6600","ffaa00","ffff00"),
    "Aurora":          ("00ff44","00ccff","aa00ff","ff00aa"),
    "Fire":            ("ff0000","ff4400","ff8800","ffaa00"),
    "Galaxy":          ("1a0033","4400aa","8800ff","cc44ff"),
    "Neon":            ("ff00ff","aa00ff","0044ff","00ffff"),
    # ── Profile-matched ──────────────────────────────────────────────────────
    "Quiet (Blue)":    ("001aff","0033ff","004cff","0033ff"),
    "Performance (Red)":("ff0000","ff0000","ff0000","ff0000"),
    "Custom (Pink)":   ("ff69b4","ff69b4","ff69b4","ff69b4"),
    # ── Special ──────────────────────────────────────────────────────────────
    "Rainbow":         ("ff0000","ff8800","0044ff","aa00ff"),
    "Stealth":         ("111111","111111","111111","111111"),
    "Off":             ("000000","000000","000000","000000"),
}

# Detect actual profile names from the kernel (low-power vs quiet)
def _detect_profiles():
    return ["quiet", "balanced", "performance", "custom"]

PROFILES       = _detect_profiles()

# UI labels — "low-power" is the sysfs name but user always sees "Quiet"
PROFILE_LABELS = {
    "quiet":       "Quiet",
    "balanced":    "Balanced",
    "performance": "Performance",
    "custom":      "Custom",
}
PROFILE_ICONS = {
    "quiet":       "🔵",
    "balanced":    "⚪",
    "performance": "🔴",
    "custom":      "🩷",
}
PROFILE_DESCS = {
    "quiet":       "15W · Boost OFF",
    "balanced":    "35W · Boost ON",
    "performance": "54W · Boost ON",
    "custom":      "54W · Custom Config",
}
PROFILE_COLORS = {
    "quiet":       "#4a9eff",
    "balanced":    "#d0d0d0",
    "performance": "#ff4757",
    "custom":      "#ff69b4",
}

EPP_VALUES = ["default","performance","balance_performance","balance_power","power"]
EPP_LABELS = {"default":"Default","performance":"Performance",
              "balance_performance":"Balance Performance",
              "balance_power":"Balance Power","power":"Power Save"}

_THEMES = {
    "dark": {
        "C_BG":      "#0d0d0d",
        "C_SIDEBAR": "#111111",
        "C_CARD":    "#181818",
        "C_CARD2":   "#222222",
        "C_BORDER":  "#2a2a2a",
        "C_TEXT":    "#e5e5e5",
        "C_TEXT2":   "#999999",
        "C_TEXT3":   "#666666",
        "C_HOVER":   "#1a1a1a",
        "C_ACTIVE":  "#252525",
        "C_SHADOW":  "#000000",
    },
    "dark_dimmed": {
        "C_BG":      "#151515",
        "C_SIDEBAR": "#1a1a1a",
        "C_CARD":    "#1e1e1e",
        "C_CARD2":   "#262626",
        "C_BORDER":  "#333333",
        "C_TEXT":    "#e0e0e0",
        "C_TEXT2":   "#999999",
        "C_TEXT3":   "#666666",
        "C_HOVER":   "#222222",
        "C_ACTIVE":  "#2d2d2d",
        "C_SHADOW":  "#000000",
    },
    "oled_black": {
        "C_BG":      "#000000",
        "C_SIDEBAR": "#050505",
        "C_CARD":    "#0a0a0a",
        "C_CARD2":   "#111111",
        "C_BORDER":  "#1a1a1a",
        "C_TEXT":    "#e0e0e0",
        "C_TEXT2":   "#888888",
        "C_TEXT3":   "#555555",
        "C_HOVER":   "#0f0f0f",
        "C_ACTIVE":  "#151515",
        "C_SHADOW":  "#000000",
    },
    "light": {
        "C_BG":      "#f5f5f5",
        "C_SIDEBAR": "#eaeaea",
        "C_CARD":    "#ffffff",
        "C_CARD2":   "#f0f0f0",
        "C_BORDER":  "#e0e0e0",
        "C_TEXT":    "#1a1a1a",
        "C_TEXT2":   "#666666",
        "C_TEXT3":   "#999999",
        "C_HOVER":   "#eeeeee",
        "C_ACTIVE":  "#e5e5e5",
        "C_SHADOW":  "#cccccc",
    },
}

def _load_theme_colours():
    global C_BG, C_SIDEBAR, C_CARD, C_CARD2, C_BORDER, C_TEXT, C_TEXT2, C_TEXT3
    global C_HOVER, C_ACTIVE, C_SHADOW
    try:
        cfg = json.loads(APP_CFG.read_text()) if APP_CFG.exists() else {}
        t = _THEMES.get(cfg.get("theme","dark"), _THEMES["dark"])
    except:
        t = _THEMES["dark"]
    C_BG     = t["C_BG"]
    C_SIDEBAR= t["C_SIDEBAR"]
    C_CARD   = t["C_CARD"]
    C_CARD2  = t["C_CARD2"]
    C_BORDER = t["C_BORDER"]
    C_TEXT   = t["C_TEXT"]
    C_TEXT2  = t["C_TEXT2"]
    C_TEXT3  = t["C_TEXT3"]
    C_HOVER  = t["C_HOVER"]
    C_ACTIVE = t["C_ACTIVE"]
    C_SHADOW = t["C_SHADOW"]

_load_theme_colours()

C_ACCENT = "#cc3333"
C_GREEN  = "#4ecb71"
C_BLUE   = "#4a9eff"
C_ORANGE = "#ffa724"
C_RED    = "#ff4757"
C_PURPLE = "#a855f7"

# ══════════════════════════════════════════════════════════════════════════════
# NOTIFICATION
# ══════════════════════════════════════════════════════════════════════════════
def send_notif(title: str, body: str = "", icon: str = "computer"):
    try:
        subprocess.Popen(
            ["notify-send", "-a", "Legion Toolkit", "-i", icon,
             "-t", "3000", title, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════════════════
# DATA HELPERS
# ══════════════════════════════════════════════════════════════════════════════

# Map sysfs feature names to legion-cli command names
_CLI_FEATURES = {
    "rapidcharge":        "rapid-charging",
    "conservation_mode":  "batteryconservation",
    "fn_lock":            "fnlock",
    "touchpad":           "touchpad",
    "usb_charging":       "always-on-usb-charging",
    "camera_power":       "camera-power",
    "lockfancontroller":  "lockfancontroller",
    "maximumfanspeed":    "maximumfanspeed",
}

def _cli_status(name):
    """Get status via legion-cli. Returns "1" or "0", or None on failure."""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "legion_linux.legion_cli", f"{name}-status"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            out = r.stdout.strip().lower()
            return "1" if out in ("true", "1", "enabled", "on") else "0"
    except:
        pass
    return None

def _cli_set(name, enable):
    """Enable/disable a feature via legion-cli."""
    cmd = f"{name}-enable" if enable else f"{name}-disable"
    try:
        r = subprocess.run(
            [sys.executable, "-m", "legion_linux.legion_cli", cmd],
            capture_output=True, timeout=5
        )
        return r.returncode == 0
    except:
        return False

def _cli_feature_from_path(path):
    """Check if a path matches a CLI-supported feature. Returns CLI name or None."""
    path = str(path).lower()
    for feat_key, cli_name in _CLI_FEATURES.items():
        if feat_key in path:
            return cli_name
    return None

def rdsys(path, default="0"):
    """Read sysfs — uses legion-cli when available, falls back to direct read."""
    cli = _cli_feature_from_path(path)
    if cli:
        val = _cli_status(cli)
        if val is not None:
            return val
    try: return Path(path).read_text().strip()
    except: return default

def wrsys(path, value):
    """Write to sysfs — uses legion-cli first, then daemon, direct, pkexec."""
    import socket as _s
    path = str(path)
    value = str(value)
    # Method 1: legion-cli
    cli = _cli_feature_from_path(path)
    if cli:
        if _cli_set(cli, value == "1"):
            return
    # Method 2: daemon socket (root daemon writes with correct permissions)
    try:
        c = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
        c.settimeout(2.0)
        c.connect("/run/legion-toolkit.sock")
        c.send(f"write:{path}:{value}\n".encode())
        resp = c.recv(32).decode().strip()
        c.close()
        if resp == "ok": return
    except Exception:
        pass
    # Method 3: direct write (works if user has group permission via udev)
    try:
        Path(path).write_text(value + "\n")
        return
    except Exception:
        pass
    # Method 4: pkexec fallback
    try:
        v = value.replace("'","").replace(";","").replace("&","")
        subprocess.Popen(
            ["pkexec","sh","-c", f"echo '{v}' > {path}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception:
        pass

DAEMON_SOCKET = "/run/legion-toolkit.sock"

def apply_profile(name: str):
    """Send profile switch to daemon via unix socket. No pkexec needed."""
    import socket as _s
    try:
        c = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
        c.settimeout(2.0)
        c.connect(DAEMON_SOCKET)
        c.send(f"set:{name}\n".encode())
        resp = c.recv(32).decode().strip()
        c.close()
        if resp == "ok":
            return True, f"Profile set to {name}"
    except Exception as e:
        pass
    # Fallback: write powermode directly
    rev = {"quiet": 1, "balanced": 2, "performance": 3, "custom": 255}
    try:
        val = rev.get(name, 2)
        LEGION_POWERMODE.write_text(f"{val}\n")
        return True, f"Profile set to {name}"
    except Exception as e:
        return False, str(e)

def find_hwmon(name):
    for base in [Path("/sys/class/hwmon"),Path("/sys/devices/virtual/hwmon")]:
        if not base.exists(): continue
        try:
            for p in base.iterdir():
                nf = p/"name"
                if nf.exists() and nf.read_text().strip() == name:
                    return p
        except: pass
    return None

# ── LLL (LenovoLegionLinux) Integration ───────────────────────────────────────────────
LLL_FANCURVE_DEBUGFS = Path("/sys/kernel/debug/legion/fancurve")
FAN_FULLSPEED = LEGION_SYS_BASEPATH / "fan_fullspeed"

def is_lll_module_loaded() -> bool:
    """Check if LLL kernel module is loaded."""
    return Path("/sys/module/legion_laptop").exists()

def is_lll_device_bound() -> bool:
    """Check if LLL device is bound (hwmon exposed)."""
    return find_hwmon("legion_hwmon") is not None

def get_lll_status() -> dict:
    """Get detailed LLL status for UI display."""
    status = {
        "module_loaded": is_lll_module_loaded(),
        "device_bound": is_lll_device_bound(),
        "debugfs_exists": LLL_FANCURVE_DEBUGFS.exists(),
        "has_fancurve": False,
    }
    if status["debugfs_exists"]:
        try:
            curve = LLL_FANCURVE_DEBUGFS.read_text()
            status["has_fancurve"] = "fan curve points size:" in curve
        except: pass
    return status

def is_lll_available() -> bool:
    """Full check: module loaded AND device bound."""
    return is_lll_module_loaded() and is_lll_device_bound()

def force_load_lll() -> tuple[bool, str]:
    """Try to force-load LLL module with force=1."""
    if not is_lll_module_loaded():
        try:
            r = subprocess.run(
                ["pkexec", "modprobe", "legion_laptop", "force=1"],
                capture_output=True, text=True, timeout=15
            )
            if r.returncode == 0:
                return True, "Module loaded with force=1"
            return False, r.stderr.strip()[:80]
        except Exception as e:
            return False, str(e)[:80]
    return is_lll_device_bound(), "Module already loaded, trying force..."

def get_ic_temp() -> int:
    """Get IC temperature from LLL hwmon (returns 0 if not available)."""
    h = find_hwmon("legion_hwmon")
    if h:
        for f in [h/"temp3_input", h/"temp4_input"]:
            if f.exists():
                try: return int(f.read_text())//1000
                except: pass
    return 0

def read_fancurve_from_hw() -> str | None:
    """Read current fan curve from LLL debugfs. Returns None if not available."""
    if LLL_FANCURVE_DEBUGFS.exists():
        try: return LLL_FANCURVE_DEBUGFS.read_text()
        except: pass
    return None

def write_fancurve_to_hw(points: list[dict]) -> tuple[bool, str]:
    """Write fan curve points to LLL hwmon. Each point: {fan1_pwm, fan2_pwm, cpu_temp, gpu_temp, ic_temp, accel, decel}"""
    hwmon = _fan_hwmon()
    if not hwmon:
        return False, "LLL hwmon not found"
    
    try:
        for i, pt in enumerate(points, 1):
            if i > 10:
                break
            base = f"pwm{1 if i <= 3 else 2}_auto_point{i}_"
            
            # Write PWM values (fan speed)
            if "fan1_pwm" in pt:
                subprocess.run(["pkexec", "sh", "-c", f"echo {pt['fan1_pwm']} > {hwmon/base}pwm"],
                             capture_output=True, timeout=2)
            if "fan2_pwm" in pt:
                other = "pwm2_auto_point" if i <= 3 else "pwm1_auto_point"
                idx = i if i <= 3 else i - 3
                subprocess.run(["pkexec", "sh", "-c", f"echo {pt['fan2_pwm']} > {hwmon}{other}{idx}_pwm"],
                             capture_output=True, timeout=2)
            
            # Write temperature thresholds
            if "cpu_temp" in pt:
                subprocess.run(["pkexec", "sh", "-c", f"echo {pt['cpu_temp']} > {hwmon/base}temp"],
                             capture_output=True, timeout=2)
            
            # Write acceleration/deceleration
            if "accel" in pt:
                subprocess.run(["pkexec", "sh", "-c", f"echo {pt['accel']} > {hwmon/base}accel"],
                             capture_output=True, timeout=2)
            if "decel" in pt:
                subprocess.run(["pkexec", "sh", "-c", f"echo {pt['decel']} > {hwmon/base}decel"],
                             capture_output=True, timeout=2)
        
        return True, f"Wrote {len(points)} fan curve points"
    except Exception as e:
        return False, str(e)[:80]

def save_fancurve_to_file(points: list[dict], filename: str) -> bool:
    """Save fan curve to JSON file."""
    try:
        CFG_DIR.mkdir(parents=True, exist_ok=True)
        path = CFG_DIR / f"fancurve_{filename}.json"
        path.write_text(json.dumps(points, indent=2))
        return True
    except:
        return False

def load_fancurve_from_file(filename: str) -> list[dict] | None:
    """Load fan curve from JSON file."""
    try:
        path = CFG_DIR / f"fancurve_{filename}.json"
        if path.exists():
            return json.loads(path.read_text())
    except:
        pass
    return None

def parse_fancurve(curve_text: str) -> list[dict]:
    """Parse fancurve debugfs output into list of point dicts."""
    lines = curve_text.strip().split("\n")
    if not lines or "fan curve points size:" not in curve_text:
        return []
    points = []
    header = lines[0].split("|")
    for line in lines[2:]:
        if not line.strip(): continue
        vals = line.split()
        if len(vals) >= 12:
            points.append({
                "speed_unit": int(vals[0]),
                "fan1_rpm": int(vals[1]) * 100 if vals[0] == "3" else 0,
                "fan2_rpm": int(vals[2]) * 100 if vals[0] == "3" else 0,
                "fan1_pwm": int(vals[3]),
                "fan2_pwm": int(vals[4]),
                "accel": int(vals[5]),
                "decel": int(vals[6]),
                "cpu_min": int(vals[7]),
                "cpu_max": int(vals[8]),
                "gpu_min": int(vals[9]),
                "gpu_max": int(vals[10]),
                "ic_min": int(vals[11]),
                "ic_max": int(vals[12]) if len(vals) > 12 else 127,
            })
    return points

def get_fan_lock_status() -> bool:
    """Check if fan controller is locked (read-only, firmware level)."""
    lock_path = LEGION_SYS_BASEPATH / "lockfancontroller"
    if not lock_path.exists():
        return False
    try:
        return lock_path.read_text().strip() == "1"
    except:
        return False

def set_fan_lock(lock: bool) -> tuple[bool, str]:
    """Lock/unlock fan controller. Requires LLL."""
    lock_path = LEGION_SYS_BASEPATH / "lockfancontroller"
    if not lock_path.exists():
        if not is_lll_available():
            return False, "LLL not loaded"
        return False, "lockfancontroller not found"
    try:
        val = "1" if lock else "0"
        r = subprocess.run(
            ["pkexec", "sh", "-c", f"echo {val} > {lock_path}"],
            capture_output=True, text=True, timeout=8
        )
        if r.returncode == 0:
            return True, f"Fan controller {'locked' if lock else 'unlocked'}"
        return False, r.stderr.strip()[:80]
    except Exception as e:
        return False, str(e)[:80]

def get_minifancurve_status() -> bool:
    """Check if mini fan curve (cold) is enabled."""
    mini_path = LEGION_SYS_BASEPATH / "minifancurve"
    if not mini_path.exists():
        return False
    try:
        return mini_path.read_text().strip() == "1"
    except:
        return False

def set_minifancurve(enable: bool) -> tuple[bool, str]:
    """Enable/disable mini fan curve when cold. Requires LLL."""
    mini_path = LEGION_SYS_BASEPATH / "minifancurve"
    if not mini_path.exists():
        if not is_lll_available():
            return False, "LLL not loaded"
        return False, "minifancurve not found"
    try:
        val = "1" if enable else "0"
        r = subprocess.run(
            ["pkexec", "sh", "-c", f"echo {val} > {mini_path}"],
            capture_output=True, text=True, timeout=8
        )
        if r.returncode == 0:
            return True, f"Mini fan curve {'enabled' if enable else 'disabled'}"
        return False, r.stderr.strip()[:80]
    except Exception as e:
        return False, str(e)[:80]

def set_max_fan_speed(enable: bool) -> tuple[bool, str]:
    """Set maximum fan speed (extreme cooling mode)."""
    if not FAN_FULLSPEED.exists():
        if not is_lll_available():
            return False, "LLL not loaded"
        return False, "fan_fullspeed path not found"
    try:
        val = "1" if enable else "0"
        r = subprocess.run(
            ["pkexec", "sh", "-c", f"echo {val} > {FAN_FULLSPEED}"],
            capture_output=True, text=True, timeout=8
        )
        if r.returncode == 0:
            return True, f"Max fan {'ON' if enable else 'OFF'}"
        return False, r.stderr.strip()[:80]
    except Exception as e:
        return False, str(e)[:80]

def get_cpu_temp():
    h = find_hwmon("k10temp")
    if h:
        for f in sorted(h.glob("temp*_input")):
            try: return int(f.read_text())//1000
            except: pass
    return 0

def get_fan_rpm():
    h = find_hwmon("legion_hwmon"); fans = []
    if h:
        for f in sorted(h.glob("fan*_input")):
            try: fans.append(int(f.read_text()))
            except: pass
    while len(fans) < 2: fans.append(0)
    return fans[0], fans[1]

def get_cpu_freq_ghz():
    try:
        return round(int(Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
                         .read_text()) / 1_000_000, 2)
    except: return 0.0

def get_cpu_max_freq_mhz():
    """Max allowed frequency in MHz (scaling_max_freq)."""
    try:
        return int(Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq")
                   .read_text()) // 1000
    except: return 0

def get_cpu_hw_max_mhz():
    """Hardware max frequency in MHz — scan all cores, take the highest."""
    best = 0
    for i in range(16):
        p = Path(f"/sys/devices/system/cpu/cpu{i}/cpufreq/cpuinfo_max_freq")
        try:
            v = int(p.read_text()) // 1000
            if v > best: best = v
        except: break
    # cpuinfo_max_freq can return base clock on some AMD configs;
    # also check policy0 bios_limit for actual turbo ceiling
    try:
        bl = int(Path("/sys/devices/system/cpu/cpufreq/policy0/bios_limit")
                 .read_text()) // 1000
        if bl > best: best = bl
    except: pass
    return best if best >= 3000 else 4400   # 5800H absolute fallback

def get_cpu_power_w():
    """
    NOT called directly — use DataSampler which tracks RAPL energy delta.
    This is kept as a helper for finding the energy file.
    """
    return None   # handled by _read_cpu_power_delta in DataSampler

def _find_rapl_energy_file():
    """Find AMD RAPL package energy file. Returns Path or None."""
    try:
        pc = Path("/sys/class/powercap")
        if pc.exists():
            for p in sorted(pc.iterdir()):
                try:
                    name = (p/"name").read_text().strip().lower()
                    if "package" in name or "psys" in name:
                        ef = p/"energy_uj"
                        if ef.exists(): return ef
                except: pass
    except: pass
    # AMD hwmon fallback
    h = find_hwmon("k10temp")
    if h:
        for f in h.glob("power*_input"):
            return f   # already in µW, not cumulative
    return None

def get_igpu_power_w():
    """AMD iGPU power via amdgpu hwmon or apu_power in k10temp."""
    # Try amdgpu hwmon
    h = find_hwmon("amdgpu")
    if h:
        for f in h.glob("power*_input"):
            try: return round(int(f.read_text()) / 1_000_000, 1)
            except: pass
    # Try k10temp APU power (some AMD mobile CPUs expose this)
    h2 = find_hwmon("k10temp")
    if h2:
        for f in h2.glob("power*_input"):
            try: return round(int(f.read_text()) / 1_000_000, 1)
            except: pass
    return None

def get_ram_info():
    """
    Returns (used_mb, total_mb, pct).
    Uses MemTotal - MemAvailable — the kernel's own best estimate of
    used memory, matching what htop/free shows as 'used'.
    """
    try:
        d = {}
        with open("/proc/meminfo") as f:
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    d[k.strip()] = int(v.strip().split()[0])
        total     = d.get("MemTotal", 0)
        available = d.get("MemAvailable", 0)
        used      = max(0, total - available)
        pct       = int(used * 100 / max(total, 1))
        return used // 1024, total // 1024, pct
    except: return 0, 0, 0

def get_battery_pct():
    try:
        n = int(rdsys(BAT/"energy_now"))
        f = int(rdsys(BAT/"energy_full", "1"))
        return min(100, int(n * 100 / f))
    except: return 0

def get_battery_status(): return rdsys(BAT/"status", "Unknown")

def get_battery_health():
    try:
        f = int(rdsys(BAT/"energy_full","1"))
        d = int(rdsys(BAT/"energy_full_design","1"))
        return min(100, int(f*100/d))
    except: return 0

def get_battery_stats():
    s = {}
    s["percent"] = get_battery_pct()
    s["status"]  = get_battery_status()
    s["health"]  = get_battery_health()
    s["cycles"]  = rdsys(BAT/"cycle_count","—")

    # ── Battery temperature — exhaustive scan ────────────────────────────────
    _bat_temp = None

    # 1. Direct BAT sysfs (some kernels expose this)
    for bat_path in [BAT, Path("/sys/class/power_supply/BAT1"),
                     Path("/sys/class/power_supply/CMB0")]:
        for fname in ["temp", "temp_now"]:
            try:
                v = int((bat_path/fname).read_text().strip())
                if v > 0:
                    # Values can be in tenths of °C (273→27) or milli-°C
                    _bat_temp = v // 10 if v > 1000 else v
                    break
            except: pass
        if _bat_temp is not None: break

    # 2. power_supply device symlink — real device path often has temp
    if _bat_temp is None:
        try:
            real = Path("/sys/class/power_supply/BAT0").resolve()
            for fname in ["temp", "temp_now", "uevent"]:
                p = real / fname
                if fname == "uevent" and p.exists():
                    # parse POWER_SUPPLY_TEMP= from uevent
                    for line in p.read_text().splitlines():
                        if "TEMP=" in line:
                            try:
                                v = int(line.split("=")[1].strip())
                                if v > 0:
                                    _bat_temp = v // 10 if v > 1000 else v
                            except: pass
                elif p.exists():
                    try:
                        v = int(p.read_text().strip())
                        if v > 0:
                            _bat_temp = v // 10 if v > 1000 else v
                            break
                    except: pass
        except: pass

    # 3. hwmon scan — look for battery/acpi named hwmon devices
    if _bat_temp is None:
        try:
            for hwmon in sorted(Path("/sys/class/hwmon").iterdir()):
                try:
                    name = (hwmon/"name").read_text().strip().lower()
                except: name = ""
                if not any(k in name for k in
                           ("bat","acpi","power","bq","max","lenovo","smbus")):
                    continue
                for f in sorted(hwmon.glob("temp*_input")):
                    try:
                        v = int(f.read_text().strip()) // 1000
                        if 10 < v < 80:
                            _bat_temp = v; break
                    except: pass
                if _bat_temp is not None: break
        except: pass

    # 4. Scan ALL hwmon for a temp in battery range (20–55°C realistic)
    if _bat_temp is None:
        try:
            for hwmon in sorted(Path("/sys/class/hwmon").iterdir()):
                try: name = (hwmon/"name").read_text().strip().lower()
                except: name = ""
                # Skip CPU/GPU hwmon — not battery temps
                if any(k in name for k in ("k10temp","coretemp","nct","asus",
                                           "it8","gpu","nouveau","radeon","amdgpu")):
                    continue
                for f in sorted(hwmon.glob("temp*_input")):
                    try:
                        v = int(f.read_text().strip()) // 1000
                        if 20 < v < 55:  # realistic battery temp range
                            _bat_temp = v; break
                    except: pass
                if _bat_temp is not None: break
        except: pass

    # 5. ACPI thermal zone — last resort
    if _bat_temp is None:
        try:
            for tz in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
                try:
                    ttype = (tz/"type").read_text().strip().lower()
                    if any(k in ttype for k in ("bat","acpi","charger")):
                        v = int((tz/"temp").read_text().strip()) // 1000
                        if 10 < v < 80:
                            _bat_temp = v; break
                except: pass
        except: pass

    s["temp"] = f"{_bat_temp} °C" if _bat_temp is not None else "—"

    try: s["power"] = f"{int(rdsys(BAT/'power_now','0'))/1_000_000:.1f} W"
    except: s["power"] = "—"
    try: s["voltage"] = f"{int(rdsys(BAT/'voltage_now','0'))/1_000_000:.2f} V"
    except: s["voltage"] = "—"
    try:
        ef = int(rdsys(BAT/"energy_full","0"))
        ed = int(rdsys(BAT/"energy_full_design","0"))
        s["capacity"] = f"{ef//1000} mWh / {ed//1000} mWh (design)"
    except: s["capacity"] = "—"
    s["manufacturer"] = rdsys(BAT/"manufacturer","—")
    s["model"]        = rdsys(BAT/"model_name","—")
    s["technology"]   = rdsys(BAT/"technology","—")
    return s

def get_epp():
    try:
        return Path("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference") \
               .read_text().strip()
    except: return "default"

def set_epp(val):
    paths = [
        f"/sys/devices/system/cpu/cpu{i}/cpufreq/energy_performance_preference"
        for i in range(32)
        if Path(f"/sys/devices/system/cpu/cpu{i}/cpufreq/energy_performance_preference").exists()
    ]
    if paths:
        cmd = " && ".join(f"echo {val} > {p}" for p in paths)
        subprocess.Popen(["pkexec","sh","-c",cmd],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def get_governor():
    try:
        return Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read_text().strip()
    except: return "—"

def get_ac_connected():
    try:
        for psu in Path("/sys/class/power_supply").iterdir():
            if rdsys(psu/"type","") == "Mains":
                return rdsys(psu/"online","0") == "1"
    except: pass
    return False

def get_ai_engine():
    """Return '1'/'0' for AI Engine state, or None if unavailable."""
    if AI_ENGINE:
        return rdsys(AI_ENGINE, "0")
    return None

def set_ai_engine(enabled: bool):
    if AI_ENGINE:
        wrsys(AI_ENGINE, "1" if enabled else "0")
        return True
    # Fallback: use EPP balance_performance
    if enabled:
        set_epp("balance_performance")
    else:
        set_epp("default")
    return False   # not native, EPP fallback

# ── GPU via nvidia-smi ────────────────────────────────────────────────────────
_gpu_cache  = {}
_gpu_last   = 0.0
_GPU_LOCK   = threading.Lock()

def get_gpu_info():
    global _gpu_cache, _gpu_last
    with _GPU_LOCK:
        now = time.time()
        if now - _gpu_last < 1.4:
            return _gpu_cache
        try:
            out = subprocess.check_output(
                ["nvidia-smi",
                 "--query-gpu=utilization.gpu,temperature.gpu,clocks.current.graphics,"
                 "memory.used,memory.total,pstate,power.draw,name",
                 "--format=csv,noheader,nounits"],
                stderr=subprocess.DEVNULL, text=True, timeout=2
            ).strip().split(",")
            if len(out) >= 8:
                _gpu_cache = {
                    "util":      int(out[0].strip()),
                    "temp":      int(out[1].strip()),
                    "freq":      int(out[2].strip()),
                    "mem_used":  int(out[3].strip()),
                    "mem_total": int(out[4].strip()),
                    "pstate":    out[5].strip(),
                    "power":     float(out[6].strip()),
                    "name":      out[7].strip(),
                    "available": True,
                }
                _gpu_last = now
                return _gpu_cache
        except: pass
        _gpu_cache = {"available": False}
        _gpu_last  = now
        return _gpu_cache

# ── Display / VRR / Refresh Rate (KDE Plasma 6 Wayland via kscreen-doctor) ─────

def _kscreen_json():
    """Return parsed kscreen-doctor JSON, or {}."""
    try:
        out = subprocess.check_output(
            ["kscreen-doctor", "-j"], stderr=subprocess.DEVNULL, text=True, timeout=3
        )
        return json.loads(out)
    except: return {}

def get_display_outputs():
    """
    Return list of (output_name, current_mode_str, modes_list).
    modes_list = [(mode_str, is_current), ...]  mode_str = 'WxH@HZ'
    Uses kscreen-doctor (works on KDE Plasma 6 Wayland).
    """
    outputs = []
    try:
        data = _kscreen_json()
        for o in data.get("outputs", []):
            if not o.get("enabled"): continue
            name = o.get("name", "")
            cur_id = o.get("currentModeId","")
            modes = []
            cur_mode = ""
            for m in o.get("modes", []):
                mid   = m.get("id","")
                size  = m.get("size", {})
                w, h  = size.get("width",0), size.get("height",0)
                # refreshRate is a float like 143.981
                hz    = round(m.get("refreshRate", 0))
                if not (w and h and hz): continue
                mode_str = f"{w}x{h}@{hz}"
                is_cur   = (mid == cur_id)
                modes.append((mode_str, is_cur))
                if is_cur: cur_mode = mode_str
            if modes:
                outputs.append((name, cur_mode, modes))
    except: pass
    return outputs

def _kscreen_output_idx(name: str) -> int:
    """Return 1-based output index for kscreen-doctor output.N commands."""
    try:
        data = _kscreen_json()
        for i, o in enumerate(data.get("outputs", []), 1):
            if o.get("name","") == name:
                return i
    except: pass
    return 1

def get_vrr_status():
    """
    Return (is_on, policy_int) for the first enabled output.
    policy: 0=never  1=always  2=automatic
    """
    try:
        data = _kscreen_json()
        for o in data.get("outputs", []):
            if o.get("enabled"):
                vrr = o.get("vrrpolicy", 0)
                return vrr in (1, 2), vrr
        return False, 0
    except: return False, -1

def _persist_vrr(output_name: str, policy: int):
    """
    Write VRR policy into every kscreen config file that references this output,
    so the setting survives reboot. Then ask KWin to reload.
    policy: 0=never  1=always  2=automatic
    """
    kscreen_dir = Path.home() / ".local/share/kscreen"
    if not kscreen_dir.exists(): return
    changed = False
    try:
        for cfg_file in kscreen_dir.glob("*"):
            if cfg_file.is_dir(): continue
            try:
                data = json.loads(cfg_file.read_text())
                for o in data.get("outputs", []):
                    if o.get("name","") == output_name:
                        o["vrrpolicy"] = policy
                        changed = True
                if changed:
                    cfg_file.write_text(json.dumps(data, indent=2))
                    changed = False   # reset for next file
            except: pass
    except: pass
    # Signal KWin to reload display config
    try:
        subprocess.Popen(
            ["dbus-send","--session","--type=signal","/KWin",
             "org.kde.KWin.reloadConfig"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except: pass

def _ensure_nvidia_modeset():
    """
    Ensure nvidia-drm.modeset=1 is set — required for VRR on NVIDIA hybrid Wayland.
    Writes to /etc/modprobe.d/nvidia-drm.conf (needs pkexec, one-time).
    """
    cfg = Path("/etc/modprobe.d/nvidia-drm.conf")
    try:
        content = cfg.read_text() if cfg.exists() else ""
        if "modeset=1" in content: return True   # already set
    except: pass
    try:
        line = "options nvidia-drm modeset=1 fbdev=1\n"
        subprocess.run(
            ["pkexec","sh","-c",f"echo '{line.strip()}' > /etc/modprobe.d/nvidia-drm.conf"],
            timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return True
    except: return False

def _configure_kwin_vrr(enabled: bool):
    """
    Set KWin compositor VRR policy via kwriteconfig6.
    This is what KDE System Settings → Display → VRR does internally.
    """
    try:
        # VRRPolicy: 0=Never 1=Automatic 2=Always
        policy = "1" if enabled else "0"
        subprocess.run(
            ["kwriteconfig6","--file","kwinrc",
             "--group","Compositing","--key","VRRPolicy", policy],
            timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        # Reload KWin compositor (non-blocking, no visual glitch)
        subprocess.Popen(
            ["qdbus6","org.kde.KWin","/Compositor","org.kde.kwin.Compositing.resume"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        # Also signal via dbus
        subprocess.Popen(
            ["dbus-send","--session","--dest=org.kde.KWin",
             "--type=method_call","/KWin","org.kde.KWin.reconfigure"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except: pass

def set_vrr(enabled: bool, output_name: str = ""):
    """
    Enable/disable VRR on KDE Plasma 6 Wayland + NVIDIA hybrid.
    1. kscreen-doctor output.N.vrrpolicy.automatic — per-output, immediate
    2. kwriteconfig6 kwinrc VRRPolicy — KWin compositor global policy
    3. Patch ~/.local/share/kscreen/ files — survives reboot
    4. nvidia-drm.modeset=1 — required for NVIDIA hybrid VRR
    """
    policy     = "automatic" if enabled else "never"
    policy_int = 2 if enabled else 0
    errors = []

    try:
        data = _kscreen_json()
        outputs = data.get("outputs", [])
        targets = ([o for o in outputs if o.get("name","") == output_name]
                   if output_name else
                   [o for o in outputs if o.get("enabled")])
        if not targets: targets = outputs

        for o in targets:
            name = o.get("name","")
            idx  = _kscreen_output_idx(name)
            try:
                r = subprocess.run(
                    ["kscreen-doctor", f"output.{idx}.vrrpolicy.{policy}"],
                    capture_output=True, text=True, timeout=5
                )
                if r.returncode != 0 and r.stderr:
                    errors.append(r.stderr.strip())
            except Exception as e: errors.append(str(e))
            _persist_vrr(name, policy_int)

        # KWin compositor VRR policy
        _configure_kwin_vrr(enabled)

        # Ensure nvidia-drm modeset (one-time, silent)
        if enabled: _ensure_nvidia_modeset()

        msg = ("Automatic — syncs to GPU framerate" if enabled else "Fixed refresh rate")
        if errors: msg += f"\n⚠ {errors[0]}"
        send_notif("VRR " + ("Enabled ✓" if enabled else "Disabled"), msg, "display")

    except Exception as e:
        send_notif("VRR Error", str(e), "dialog-error")

def set_refresh_rate(output: str, mode: str):
    """
    Set display mode via kscreen-doctor (works on KDE Plasma 6 Wayland).
    mode = 'WxH@HZ'
    """
    try:
        # Find the exact mode id from kscreen-doctor JSON
        data = _kscreen_json()
        mode_id = None
        for o in data.get("outputs", []):
            if o.get("name","") == output:
                res_part, hz_part = mode.split("@")
                w_str, h_str = res_part.split("x")
                w, h, hz = int(w_str), int(h_str), int(hz_part)
                for m in o.get("modes", []):
                    sz = m.get("size",{})
                    mhz = round(m.get("refreshRate",0))
                    if sz.get("width")==w and sz.get("height")==h and mhz==hz:
                        mode_id = m.get("id","")
                        break
                break
        idx = _kscreen_output_idx(output)
        if mode_id:
            subprocess.Popen(
                ["kscreen-doctor", f"output.{idx}.mode.{mode_id}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        else:
            # Fallback: try WxHzHZ format
            subprocess.Popen(
                ["kscreen-doctor", f"output.{idx}.mode.{mode}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        send_notif("Refresh Rate Changed",
                   f"{output}: {mode.replace('@',' @ ')} Hz", "display")
    except Exception as e:
        send_notif("Refresh Rate Error", str(e), "dialog-error")
# ── RGB Keyboard — via legionaura CLI ─────────────────────────────────────────
# legionaura (AUR: legionaura) wraps the USB HID protocol for Legion keyboards.
# Install: yay -S legionaura
# CLI: legionaura static|breath|wave|hue|off [colors] [--speed 1-4] [--brightness 1-2]

_KBD_BRI_PATH = Path("/sys/class/leds/platform::kbd_backlight/brightness")
_KBD_BRI_MAX  = Path("/sys/class/leds/platform::kbd_backlight/max_brightness")

def _has_legionaura() -> bool:
    try:
        r = subprocess.run(["which","legionaura"], capture_output=True, timeout=2)
        return r.returncode == 0
    except Exception: return False

def _legionaura_version() -> str:
    try:
        r = subprocess.run(["legionaura","--version"], capture_output=True, text=True, timeout=2)
        return (r.stdout or r.stderr).strip().split("\n")[0][:40]
    except Exception: return "unknown"

def _write_sysfs(path: Path, value: str) -> bool:
    try:
        path.write_text(value + "\n"); return True
    except PermissionError: pass
    except Exception: return False
    try:
        import socket as _s
        c = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM); c.settimeout(2.0)
        c.connect("/run/legion-toolkit.sock")
        c.send(f"write:{path}:{value}\n".encode())
        r = c.recv(32).decode().strip(); c.close()
        if r == "ok": return True
    except Exception: pass
    try:
        v = value.replace("'","").replace(";","").replace("&","")
        subprocess.run(["pkexec","sh","-c",f"printf '%s\\n' '{v}' > {path}"],
                       check=False, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception: return False

def set_kbd_brightness(val: int):
    p = _KBD_BRI_PATH if _KBD_BRI_PATH.exists() else KBD_BACKLIGHT_PATH
    if p: _write_sysfs(p, str(val))

def get_kbd_brightness() -> int:
    p = _KBD_BRI_PATH if _KBD_BRI_PATH.exists() else KBD_BACKLIGHT_PATH
    try: return int(p.read_text().strip()) if p else 0
    except: return 0

def get_kbd_max_brightness() -> int:
    try: return int(_KBD_BRI_MAX.read_text().strip()) if _KBD_BRI_MAX.exists() else 2
    except: return 2

# ── Y-Logo and IO-Port LED (via LLL) ─────────────────────────────────────────
_YLOGO_PATH = Path("/sys/class/leds/platform::ylogo/brightness")
_IOPORT_PATH = Path("/sys/class/leds/platform::ioport/brightness")

def set_ylogo_brightness(brightness: int) -> tuple[bool, str]:
    """Set Y-logo LED brightness (0-2 or 0-100 depending on model). Requires LLL."""
    if not _YLOGO_PATH.exists():
        return False, "Y-logo LED not found"
    try:
        _YLOGO_PATH.write_text(str(brightness) + "\n")
        return True, f"Y-logo brightness: {brightness}"
    except PermissionError:
        try:
            subprocess.run(["pkexec", "sh", "-c", f"echo {brightness} > {_YLOGO_PATH}"],
                         capture_output=True, timeout=5)
            return True, f"Y-logo brightness: {brightness}"
        except Exception as e:
            return False, str(e)[:80]
    except Exception as e:
        return False, str(e)[:80]

def get_ylogo_brightness() -> int:
    """Get current Y-logo LED brightness."""
    if not _YLOGO_PATH.exists():
        return 0
    try:
        return int(_YLOGO_PATH.read_text().strip())
    except:
        return 0

def set_ioport_brightness(brightness: int) -> tuple[bool, str]:
    """Set IO-Port LED brightness. Requires LLL."""
    if not _IOPORT_PATH.exists():
        return False, "IO-Port LED not found"
    try:
        _IOPORT_PATH.write_text(str(brightness) + "\n")
        return True, f"IO-Port brightness: {brightness}"
    except PermissionError:
        try:
            subprocess.run(["pkexec", "sh", "-c", f"echo {brightness} > {_IOPORT_PATH}"],
                         capture_output=True, timeout=5)
            return True, f"IO-Port brightness: {brightness}"
        except Exception as e:
            return False, str(e)[:80]
    except Exception as e:
        return False, str(e)[:80]

def get_ioport_brightness() -> int:
    """Get current IO-Port LED brightness."""
    if not _IOPORT_PATH.exists():
        return 0
    try:
        return int(_IOPORT_PATH.read_text().strip())
    except:
        return 0

def run_legionaura(args: list, callback=None):
    """Run legionaura CLI in a background thread. callback(ok, msg) when done."""
    def _do():
        try:
            r = subprocess.run(
                ["legionaura"] + args,
                capture_output=True, text=True, timeout=8
            )
            ok  = r.returncode == 0
            msg = (r.stdout or r.stderr or "").strip()[:120]
            if not msg: msg = "OK" if ok else "failed (no output)"
        except FileNotFoundError:
            ok, msg = False, "legionaura not found — install: yay -S legionaura"
        except Exception as e:
            ok, msg = False, str(e)[:120]
        if callback: callback(ok, msg)
    threading.Thread(target=_do, daemon=True).start()

# ── Overclock helpers ─────────────────────────────────────────────────────────
def load_oc_config():
    try:
        if OC_CFG.exists():
            return json.loads(OC_CFG.read_text())
    except: pass
    hw_max = get_cpu_hw_max_mhz()
    return {
        "cpu_max_freq_mhz": hw_max,
        "gpu_core_offset":  0,
        "gpu_mem_offset":   0,
        "gpu_power_limit":  0,
    }

def save_oc_config(data):
    try:
        OC_CFG.parent.mkdir(parents=True, exist_ok=True)
        OC_CFG.write_text(json.dumps(data, indent=2))
    except: pass

def apply_cpu_freq(mhz: int):
    """Set scaling_max_freq for all CPU cores."""
    khz = mhz * 1000
    paths = [
        f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_max_freq"
        for i in range(32)
        if Path(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_max_freq").exists()
    ]
    if paths:
        cmd = " && ".join(f"echo {khz} > {p}" for p in paths)
        subprocess.Popen(["pkexec","sh","-c",cmd],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        send_notif("CPU Frequency Set", f"Max frequency: {mhz} MHz", "cpu")

def apply_gpu_oc(core_off: int, mem_off: int, power_limit: int):
    """Apply GPU overclock via nvidia-smi. Requires coolbits=28 in xorg."""
    cmds = []
    if core_off != 0:
        cmds.append(f"nvidia-smi --lock-gpu-clocks={core_off},{core_off}")
    if mem_off != 0:
        cmds.append(f"nvidia-smi --lock-memory-clocks={mem_off},{mem_off}")
    if power_limit > 0:
        cmds.append(f"nvidia-smi -pl {power_limit}")
    if cmds:
        for cmd in cmds:
            try:
                subprocess.Popen(cmd.split(),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except: pass
        send_notif("GPU Overclock Applied",
                   f"Core +{core_off} MHz | Mem +{mem_off} MHz | PL {power_limit}W",
                   "gpu")

def reset_gpu_oc():
    try:
        subprocess.Popen(["nvidia-smi","--reset-gpu-clocks"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen(["nvidia-smi","--reset-memory-clocks"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        send_notif("GPU OC Reset", "Clock offsets cleared", "gpu")
    except: pass

# ── GPU overclock via nvidia-settings (offset mode, needs coolbits=28) ────────
def apply_gpu_oc_full(core_off: int, mem_off: int, power_limit_w: int,
                      temp_target: int = 0, fan_pct: int = 0):
    errors = []
    # 1. Power limit via nvidia-smi
    if power_limit_w > 0:
        try:
            r = subprocess.run(["nvidia-smi","-i","0","-pl",str(power_limit_w)],
                               capture_output=True, text=True, timeout=5)
            if r.returncode != 0: errors.append(f"PL: {r.stderr.strip()[:60]}")
        except Exception as e: errors.append(str(e))
    # 2. Clock offsets via nvidia-settings
    if core_off != 0:
        try:
            subprocess.Popen(
                ["nvidia-settings","-a",
                 f"[gpu:0]/GPUGraphicsClockOffsetAllPerformanceLevels={core_off}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except: errors.append("nvidia-settings not found")
    if mem_off != 0:
        try:
            subprocess.Popen(
                ["nvidia-settings","-a",
                 f"[gpu:0]/GPUMemoryTransferRateOffsetAllPerformanceLevels={mem_off}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except: pass
    # 3. Temperature target
    if temp_target > 0:
        try:
            subprocess.Popen(["nvidia-smi","-i","0",
                               f"--gom={temp_target}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except: pass
    # 4. Manual fan speed
    if fan_pct > 0:
        try:
            subprocess.Popen(
                ["nvidia-settings","-a","[gpu:0]/GPUFanControlState=1",
                 "-a",f"[fan:0]/GPUTargetFanSpeed={fan_pct}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except: pass
    msg = f"Core +{core_off} MHz | Mem +{mem_off} MHz | PL {power_limit_w}W"
    if errors: msg += f" ⚠ {errors[0]}"
    send_notif("GPU OC Applied", msg, "gpu")

def reset_gpu_oc_full():
    try:
        subprocess.Popen(["nvidia-smi","--reset-gpu-clocks"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen(["nvidia-smi","--reset-memory-clocks"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except: pass
    try:
        subprocess.Popen(
            ["nvidia-settings","-a","[gpu:0]/GPUGraphicsClockOffsetAllPerformanceLevels=0",
             "-a","[gpu:0]/GPUMemoryTransferRateOffsetAllPerformanceLevels=0",
             "-a","[gpu:0]/GPUFanControlState=0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except: pass
    send_notif("GPU OC Reset", "All values reset to default", "gpu")

# ── CPU TDP via RAPL (writable PL1/PL2) ──────────────────────────────────────
def _rapl_power_paths():
    """Return (pl1_path, pl2_path) or (None, None)."""
    try:
        pc = Path("/sys/class/powercap")
        for p in sorted(pc.iterdir()):
            try:
                name = (p/"name").read_text().strip().lower()
                if "package" in name or "psys" in name or name.startswith("amd"):
                    pl1 = p/"constraint_0_power_limit_uw"
                    pl2 = p/"constraint_1_power_limit_uw"
                    if pl1.exists(): return pl1, pl2 if pl2.exists() else None
            except: pass
    except: pass
    return None, None

def set_cpu_tdp(pl1_w: int, pl2_w: int):
    """Set CPU TDP via RAPL. Requires daemon socket or pkexec."""
    pl1_path, pl2_path = _rapl_power_paths()
    if pl1_path:
        wrsys(pl1_path, str(pl1_w * 1_000_000))
    if pl2_path and pl2_w > 0:
        wrsys(pl2_path, str(pl2_w * 1_000_000))
    send_notif("CPU TDP Set", f"PL1: {pl1_w}W  PL2: {pl2_w}W", "cpu")

def get_cpu_tdp():
    """Return (pl1_w, pl2_w) or (0, 0)."""
    pl1_path, pl2_path = _rapl_power_paths()
    try:
        pl1 = int(rdsys(pl1_path, "0")) // 1_000_000 if pl1_path else 0
        pl2 = int(rdsys(pl2_path, "0")) // 1_000_000 if pl2_path else 0
        return pl1, pl2
    except: return 0, 0

def get_cpu_min_freq_mhz():
    try:
        return int(Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_min_freq")
                   .read_text()) // 1000
    except: return 400

def apply_cpu_min_freq(mhz: int):
    khz = mhz * 1000
    paths = [f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_min_freq"
             for i in range(32)
             if Path(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_min_freq").exists()]
    if paths:
        cmd = " && ".join(f"echo {khz} > {p}" for p in paths)
        subprocess.Popen(["pkexec","sh","-c",cmd],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ── Fan control via legion_hwmon + FAN_FULLSPEED sysfs ────────────────────────
#
# Legion 5 15ACH6H fan control reality:
#   fan_fullspeed  → /sys/devices/.../PNP0C09:00/fan_fullspeed  (0/1) — always works
#   pwm1/pwm2      → legion_hwmon pwm files — may be writable on some driver versions
#   fan1_input/fan2_input → RPM read-only — always works
#
# Strategy:
#   Auto       → fan_fullspeed=0  (firmware controls fans via thermal curve)
#   Full Speed → fan_fullspeed=1  (locks both fans to 100%)
#   Manual     → try pwm via pkexec; if hwmon missing fall back to fan_fullspeed
#   Presets    → map to platform_profile + fan_fullspeed combinations

def _fan_hwmon():
    return find_hwmon("legion_hwmon")

def _fan_hwmon_info() -> dict:
    """Return dict of what the hwmon actually exposes."""
    h = _fan_hwmon()
    if not h:
        return {"found": False, "path": None,
                "pwm1": False, "pwm2": False,
                "pwm1_enable": False, "pwm2_enable": False}
    return {
        "found": True,
        "path": str(h),
        "pwm1":        (h/"pwm1").exists(),
        "pwm2":        (h/"pwm2").exists(),
        "pwm1_enable": (h/"pwm1_enable").exists(),
        "pwm2_enable": (h/"pwm2_enable").exists(),
    }

def get_fan_rpm():
    h = _fan_hwmon(); fans = []
    if h:
        for f in sorted(h.glob("fan*_input")):
            try: fans.append(int(f.read_text()))
            except: pass
    while len(fans) < 2: fans.append(0)
    return fans[0], fans[1]

def get_fan_pwm():
    h = _fan_hwmon()
    if not h: return 128, 128
    try: c = int((h/"pwm1").read_text())
    except: c = 0
    try: g = int((h/"pwm2").read_text())
    except: g = 0
    return c, g

def _write_fan_pwm(cpu_pct: int, gpu_pct: int) -> tuple:
    """
    Write PWM values as root via subprocess (avoids daemon socket path issues).
    Returns (ok: bool, msg: str).
    """
    h = _fan_hwmon()
    if not h:
        return False, "legion_hwmon not found"

    cpu_pwm = int(cpu_pct * 255 / 100)
    gpu_pwm = int(gpu_pct * 255 / 100)
    cmds = []

    pwm1_en = h / "pwm1_enable"
    pwm2_en = h / "pwm2_enable"
    pwm1    = h / "pwm1"
    pwm2    = h / "pwm2"

    if pwm1_en.exists(): cmds.append(f"echo 1 > {pwm1_en}")
    if pwm2_en.exists(): cmds.append(f"echo 1 > {pwm2_en}")
    if pwm1.exists():    cmds.append(f"echo {cpu_pwm} > {pwm1}")
    if pwm2.exists():    cmds.append(f"echo {gpu_pwm} > {pwm2}")

    if not cmds:
        return False, f"No writable pwm files in {h}"

    try:
        r = subprocess.run(
            ["pkexec", "sh", "-c", " && ".join(cmds)],
            capture_output=True, text=True, timeout=8
        )
        if r.returncode == 0:
            return True, f"PWM set: CPU {cpu_pct}%  GPU {gpu_pct}%"
        else:
            return False, r.stderr.strip()[:100]
    except Exception as e:
        return False, str(e)[:100]

def _write_fan_auto() -> tuple:
    """Set fans back to automatic (pwm_enable=2) + clear fan_fullspeed."""
    cmds = []
    h = _fan_hwmon()
    if h:
        for f in ["pwm1_enable", "pwm2_enable"]:
            p = h / f
            if p.exists(): cmds.append(f"echo 2 > {p}")

    if FAN_FULLSPEED.exists():
        cmds.append(f"echo 0 > {FAN_FULLSPEED}")

    if not cmds:
        return False, "No fan control paths found"

    try:
        r = subprocess.run(
            ["pkexec", "sh", "-c", " && ".join(cmds)],
            capture_output=True, text=True, timeout=8
        )
        return r.returncode == 0, r.stderr.strip()[:80] if r.returncode != 0 else "Auto"
    except Exception as e:
        return False, str(e)[:80]

def _write_fan_fullspeed(on: bool) -> tuple:
    """Set fan_fullspeed 0 or 1 via pkexec."""
    if not FAN_FULLSPEED.exists():
        return False, f"fan_fullspeed not found at {FAN_FULLSPEED}"
    val = "1" if on else "0"
    try:
        r = subprocess.run(
            ["pkexec", "sh", "-c", f"echo {val} > {FAN_FULLSPEED}"],
            capture_output=True, text=True, timeout=8
        )
        return r.returncode == 0, r.stderr.strip()[:80] if r.returncode != 0 else ("Full speed ON" if on else "Full speed OFF")
    except Exception as e:
        return False, str(e)[:80]

# Keep these as sync wrappers for backwards compat (used elsewhere)
def set_fan_mode_auto():
    _write_fan_auto()

def set_fan_mode_manual(cpu_pct: int, gpu_pct: int):
    _write_fan_pwm(cpu_pct, gpu_pct)

def set_fan_fullspeed(on: bool):
    _write_fan_fullspeed(on)
    if on: _write_fan_pwm(100, 100)

# Fan presets
FAN_PRESETS = {
    "Quiet":       (20, 20),
    "Balanced":    (50, 50),
    "Performance": (75, 80),
    "Turbo":       (90, 95),
    "Full Speed":  (100, 100),
}

def load_fan_config():
    try:
        if FAN_CFG.exists():
            return json.loads(FAN_CFG.read_text())
    except: pass
    return {"mode": "auto", "cpu_pct": 50, "gpu_pct": 50, "preset": "Balanced"}

def save_fan_config(data):
    try:
        FAN_CFG.parent.mkdir(parents=True, exist_ok=True)
        FAN_CFG.write_text(json.dumps(data, indent=2))
    except: pass

# ── Appearance config ─────────────────────────────────────────────────────────
_ACCENT_OPTIONS = {
    "Legion Red":    "#cc3333",
    "Electric Blue": "#2979ff",
    "Neon Green":    "#00e676",
    "Amber":         "#ffa724",
    "Purple":        "#a855f7",
    "Pink":          "#ff69b4",
    "Cyan":          "#00bcd4",
}

def load_app_config():
    try:
        if APP_CFG.exists():
            return json.loads(APP_CFG.read_text())
    except: pass
    return {"accent": "#cc3333", "font_size": 12}

def save_app_config(data):
    try:
        APP_CFG.parent.mkdir(parents=True, exist_ok=True)
        APP_CFG.write_text(json.dumps(data, indent=2))
    except: pass

def load_actions():
    try:
        if ACTIONS_CFG.exists():
            return json.loads(ACTIONS_CFG.read_text())
    except: pass
    return {"on_ac":"performance","on_battery":"balanced","auto_switch":False,
            "_last_ac": None}

def save_actions(data):
    try:
        ACTIONS_CFG.parent.mkdir(parents=True, exist_ok=True)
        ACTIONS_CFG.write_text(json.dumps(data, indent=2))
    except: pass

def apply_actions_now():
    """Read actions config and apply profile if auto_switch is on."""
    try:
        cfg = load_actions()
        if not cfg.get("auto_switch"): return
        ac = get_ac_connected()
        target = cfg["on_ac"] if ac else cfg["on_battery"]
        current = _read_powermode()
        if target != current:
            apply_profile(target)
            send_notif("Auto Profile",
                       f"{'AC connected' if ac else 'On battery'} → {PROFILE_LABELS.get(target,target)}",
                       "battery-charging" if ac else "battery")
    except: pass

# ══════════════════════════════════════════════════════════════════════════════
# BACKGROUND SAMPLER THREAD
# ══════════════════════════════════════════════════════════════════════════════
class DataSampler(QThread):
    data_ready = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.cpu_util    = 0
        self._running    = True
        self._last_idle  = 0
        self._last_total = 0
        self._last_ac    = None
        # RAPL delta tracking for CPU power
        self._rapl_file  = _find_rapl_energy_file()
        self._rapl_is_delta = (self._rapl_file and
                               "energy_uj" in str(self._rapl_file))
        self._rapl_last_uj = 0
        self._rapl_last_t  = 0.0
        # seed CPU stat
        try:
            with open("/proc/stat") as f:
                p = f.readline().split()
            self._last_idle  = int(p[4])
            self._last_total = sum(int(x) for x in p[1:])
        except: pass
        # seed RAPL
        if self._rapl_is_delta and self._rapl_file:
            try:
                self._rapl_last_uj = int(self._rapl_file.read_text())
                self._rapl_last_t  = time.monotonic()
            except: pass

    def _read_cpu_util(self):
        try:
            with open("/proc/stat") as f:
                p = f.readline().split()
            idle  = int(p[4])
            total = sum(int(x) for x in p[1:])
            di    = idle  - self._last_idle
            dt    = total - self._last_total
            self._last_idle  = idle
            self._last_total = total
            if dt > 0:
                self.cpu_util = max(0, 100 - int(di * 100 / dt))
        except: pass
        return self.cpu_util

    def _read_cpu_power(self):
        """Return CPU package power in watts using RAPL energy delta."""
        if not self._rapl_file:
            return None
        try:
            now = time.monotonic()
            val = int(self._rapl_file.read_text())
            if self._rapl_is_delta:
                dt = now - self._rapl_last_t
                if dt > 0.05 and self._rapl_last_uj > 0:
                    delta_uj = val - self._rapl_last_uj
                    if delta_uj < 0:   # counter wraparound
                        delta_uj += 2**32
                    watts = round(delta_uj / dt / 1_000_000, 1)
                    self._rapl_last_uj = val
                    self._rapl_last_t  = now
                    return watts if 0 < watts < 200 else None
                self._rapl_last_uj = val
                self._rapl_last_t  = now
                return None
            else:
                # hwmon power*_input is already instantaneous µW
                return round(val / 1_000_000, 1)
        except: return None

    def run(self):
        _tick = 0
        while self._running:
            try:
                _tick += 1
                # Always sample — these are cheap reads
                util          = self._read_cpu_util()
                ac            = get_ac_connected()
                profile       = _read_powermode()

                # Medium cost — every tick
                freq          = get_cpu_freq_ghz()
                temp          = get_cpu_temp()
                fan1, fan2    = get_fan_rpm()
                ic_temp      = get_ic_temp() if is_lll_available() else 0
                pct           = get_battery_pct()
                bat_status    = get_battery_status()

                # Slightly heavier — battery power
                try:
                    bat_power = f"{int(Path('/sys/class/power_supply/BAT0/power_now').read_text())/1_000_000:.1f} W"
                except: bat_power = "—"

                # CPU power via RAPL delta
                cpu_power = self._read_cpu_power()

                # Every 2 ticks — RAM, GPU, governor, EPP (slower changing)
                if _tick % 2 == 0 or _tick == 1:
                    ru, rt, rpct  = get_ram_info()
                    gpu           = get_gpu_info()
                    boost         = rdsys(AMD_BOOST,"0")
                    gov           = get_governor()
                    epp           = get_epp()
                    ai_engine     = get_ai_engine()
                    igpu_power    = get_igpu_power_w()
                    vrr_on, vrr_p = get_vrr_status()
                    self._cached = {
                        "ram_used": ru, "ram_total": rt, "ram_pct": rpct,
                        "gpu": gpu, "boost": boost, "gov": gov, "epp": epp,
                        "ai_engine": ai_engine, "igpu_power": igpu_power,
                        "vrr_on": vrr_on,
                    }
                else:
                    # Use cached values
                    cached = getattr(self, '_cached', {})
                    ru     = cached.get("ram_used",  "—")
                    rt     = cached.get("ram_total",  "—")
                    rpct   = cached.get("ram_pct",    0)
                    gpu    = cached.get("gpu",        {})
                    boost  = cached.get("boost",      "0")
                    gov    = cached.get("gov",        "—")
                    epp    = cached.get("epp",        "—")
                    ai_engine  = cached.get("ai_engine",  False)
                    igpu_power = cached.get("igpu_power", None)
                    vrr_on     = cached.get("vrr_on",     False)

                self.data_ready.emit({
                    "cpu_util":   util,  "cpu_freq":  freq,    "cpu_temp":  temp,
                    "ic_temp":    ic_temp,
                    "fan1":       fan1,  "fan2":      fan2,
                    "ram_used":   ru,    "ram_total": rt,      "ram_pct":   rpct,
                    "bat_pct":    pct,   "bat_status":bat_status,"bat_power":bat_power,
                    "boost":      boost, "gov":       gov,     "epp":       epp,
                    "ac":         ac,    "profile":   profile, "gpu":       gpu,
                    "cpu_power":  cpu_power,  "igpu_power": igpu_power,
                    "ai_engine":  ai_engine,  "vrr_on":     vrr_on,
                })

                # auto profile switch on AC change
                if ac != self._last_ac and self._last_ac is not None:
                    apply_actions_now()
                self._last_ac = ac

            except Exception:
                pass
            time.sleep(1.0)

    def stop(self):
        self._running = False
        self.wait()

# ══════════════════════════════════════════════════════════════════════════════
# WIDGET PRIMITIVES
# ══════════════════════════════════════════════════════════════════════════════

class BarFill(QWidget):
    """Smooth animated progress bar."""
    def __init__(self, pct=0, color=None, parent=None):
        super().__init__(parent)
        self._pct   = float(max(0, min(100, pct)))
        self._color = color or C_ACCENT
        self.setFixedHeight(6)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._anim = QPropertyAnimation(self, b"pct_prop", self)
        self._anim.setDuration(500)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    @pyqtProperty(float)
    def pct_prop(self): return self._pct
    @pct_prop.setter
    def pct_prop(self, v): self._pct = v; self.update()

    def set_pct(self, pct, color=None):
        target = float(max(0, min(100, pct)))
        if color: self._color = color
        if abs(target - self._pct) < 0.3:
            self._pct = target; self.update(); return
        self._anim.stop()
        self._anim.setStartValue(self._pct)
        self._anim.setEndValue(target)
        self._anim.start()

    def _bar_color(self, pct):
        if self._color != C_ACCENT: return QColor(self._color)
        if pct > 85: return QColor(C_RED)
        if pct > 65: return QColor(C_ORANGE)
        return QColor(C_ACCENT)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setBrush(QBrush(QColor(C_BORDER))); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(0, 0, w, h, 3, 3)
        fill = int(w * self._pct / 100)
        if fill > 0:
            p.setBrush(QBrush(self._bar_color(self._pct)))
            p.drawRoundedRect(0, 0, fill, h, 3, 3)
        p.end()


class StatRow(QWidget):
    def __init__(self, label, value_str="—", pct=0,
                 lbl_w=110, val_w=110, color=None, parent=None):
        super().__init__(parent)
        self.setFixedHeight(26)
        self.setStyleSheet("background:transparent;")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(10)
        self._lbl = QLabel(label)
        self._lbl.setFixedWidth(lbl_w)
        self._lbl.setStyleSheet(f"color:{C_TEXT2};font-size:12px;font-weight:500;")
        lay.addWidget(self._lbl)
        self._bar = BarFill(pct, color)
        lay.addWidget(self._bar)
        self._val = QLabel(value_str)
        self._val.setFixedWidth(val_w)
        self._val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._val.setStyleSheet(f"color:{C_TEXT};font-size:12px;font-weight:600;")
        lay.addWidget(self._val)

    def update_value(self, value_str, pct, color=None):
        self._val.setText(value_str)
        self._bar.set_pct(pct, color)

    def set_value(self, value_str, pct=0, color=None, visible=True):
        self._val.setText(value_str)
        self._bar.set_pct(pct, color)
        self.setVisible(visible)


class InfoRow(QWidget):
    def __init__(self, label, value="—", lbl_w=180, parent=None):
        super().__init__(parent)
        self.setFixedHeight(40)
        self.setStyleSheet("background:transparent;")
        lay = QHBoxLayout(self); lay.setContentsMargins(0,4,0,4)
        lbl = QLabel(label); lbl.setFixedWidth(lbl_w)
        lbl.setStyleSheet(f"color:{C_TEXT2};font-size:12px;font-weight:500;")
        self._val = QLabel(value)
        self._val.setStyleSheet(f"color:{C_TEXT};font-size:12px;font-weight:500;")
        lay.addWidget(lbl); lay.addWidget(self._val); lay.addStretch()
    def set_value(self, v): self._val.setText(v)


# ══════════════════════════════════════════════════════════════════════════════
# FIRST-RUN WIZARD  (language selector + hardware detection)
# ══════════════════════════════════════════════════════════════════════════════
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QProgressBar, QListWidget, QListWidgetItem

class FirstRunWizard(QDialog):
    """
    Shown once on first launch.
    Page 0 → Language selection
    Page 1 → Hardware detection with progress
    Page 2 → Summary / done
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Legion Linux Toolkit — Setup")
        self.setFixedSize(560, 440)
        self.setWindowIcon(_legion_icon())
        self.setStyleSheet(f"""
            QDialog {{ background:{C_BG}; }}
            QLabel  {{ color:{C_TEXT}; background:transparent; }}
            QPushButton {{
                background:{C_CARD2}; color:{C_TEXT}; border:1px solid {C_BORDER};
                border-radius:8px; padding:10px 24px; font-size:13px;
            }}
            QPushButton:hover {{ background:{C_ACCENT}; color:#fff; border-color:{C_ACCENT}; }}
            QListWidget {{
                background:{C_CARD}; border:1px solid {C_BORDER};
                border-radius:10px; color:{C_TEXT}; font-size:13px;
            }}
            QListWidget::item:selected {{
                background:{C_ACCENT}; color:#fff; border-radius:6px;
            }}
            QListWidget::item:hover {{ background:{C_CARD2}; }}
        """)
        self._hw_result = {}
        self._build()

    def _build(self):
        root = QVBoxLayout(self); root.setContentsMargins(32,28,32,24); root.setSpacing(0)

        # ── Logo + App name ───────────────────────────────────────────────────
        logo_row = QHBoxLayout()
        logo_lbl = QLabel()
        import base64 as _b64
        from PyQt6.QtGui import QPixmap as _QPixmap
        pm = _QPixmap(); pm.loadFromData(_b64.b64decode(_LEGION_ICON_B64))
        logo_lbl.setPixmap(pm.scaled(44, 52, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation))
        logo_row.addWidget(logo_lbl)
        logo_row.addSpacing(14)
        app_name = QLabel("Legion Linux Toolkit")
        app_name.setStyleSheet(f"color:{C_TEXT};font-size:20px;font-weight:600;")
        logo_row.addWidget(app_name); logo_row.addStretch()
        root.addLayout(logo_row)
        root.addSpacing(20)

        # ── Stacked pages ─────────────────────────────────────────────────────
        self._stack = QStackedWidget()
        self._stack.addWidget(self._page_lang())
        self._stack.addWidget(self._page_detect())
        self._stack.addWidget(self._page_done())
        root.addWidget(self._stack, 1)

        root.addSpacing(16)

        # ── Navigation buttons ────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._back_btn = QPushButton("← Back")
        self._back_btn.setVisible(False)
        self._back_btn.clicked.connect(self._go_back)
        self._next_btn = QPushButton("Next →")
        self._next_btn.clicked.connect(self._go_next)
        self._next_btn.setStyleSheet(
            f"QPushButton{{background:{C_ACCENT};color:#fff;border:none;"
            f"border-radius:6px;padding:8px 24px;font-size:13px;font-weight:600;}}"
            f"QPushButton:hover{{background:#aa2222;}}"
        )
        btn_row.addWidget(self._back_btn); btn_row.addWidget(self._next_btn)
        root.addLayout(btn_row)

        # Page indicator dots
        self._dots = QHBoxLayout(); self._dots.setSpacing(6)
        self._dot_lbls = []
        for _ in range(3):
            d = QLabel("●"); d.setStyleSheet(f"color:{C_TEXT3};font-size:10px;")
            self._dot_lbls.append(d); self._dots.addWidget(d)
        self._dots.addStretch()
        root.addLayout(self._dots)
        self._update_dots(0)

    def _page_lang(self) -> QWidget:
        w = QWidget(); w.setStyleSheet("background:transparent;")
        lay = QVBoxLayout(w); lay.setContentsMargins(0,0,0,0); lay.setSpacing(10)
        title = QLabel("Choose Your Language")
        title.setStyleSheet(f"color:{C_TEXT};font-size:16px;font-weight:600;")
        desc = QLabel("Select the language for the interface.")
        desc.setStyleSheet(f"color:{C_TEXT2};font-size:12px;")
        lay.addWidget(title); lay.addWidget(desc); lay.addSpacing(8)

        self._lang_list = QListWidget()
        self._lang_list.setFixedHeight(220)
        # Pre-select saved or system language
        saved = _LANG
        sel_row = 0
        for i, (code, name) in enumerate(_LANG_NAMES.items()):
            item = QListWidgetItem(f"  {name}")
            item.setData(Qt.ItemDataRole.UserRole, code)
            self._lang_list.addItem(item)
            if code == saved: sel_row = i
        self._lang_list.setCurrentRow(sel_row)
        self._lang_list.itemClicked.connect(self._on_lang_select)
        lay.addWidget(self._lang_list)
        return w

    def _page_detect(self) -> QWidget:
        w = QWidget(); w.setStyleSheet("background:transparent;")
        lay = QVBoxLayout(w); lay.setContentsMargins(0,0,0,0); lay.setSpacing(10)
        self._detect_title = QLabel("Hardware Detection")
        self._detect_title.setStyleSheet(f"color:{C_TEXT};font-size:16px;font-weight:600;")
        self._detect_desc = QLabel("Scanning your device for supported features.\nThis runs once and the result is saved.")
        self._detect_desc.setStyleSheet(f"color:{C_TEXT2};font-size:12px;")
        self._detect_desc.setWordWrap(True)
        lay.addWidget(self._detect_title); lay.addWidget(self._detect_desc)
        lay.addSpacing(12)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)   # indeterminate
        self._progress.setFixedHeight(6)
        self._progress.setStyleSheet(
            f"QProgressBar{{background:{C_BORDER};border-radius:3px;border:none;}}"
            f"QProgressBar::chunk{{background:{C_ACCENT};border-radius:3px;}}"
        )
        self._progress.hide()
        lay.addWidget(self._progress)

        self._detect_status = QLabel("")
        self._detect_status.setStyleSheet(f"color:{C_TEXT3};font-size:12px;font-family:monospace;")
        self._detect_status.setWordWrap(True)
        lay.addWidget(self._detect_status)
        lay.addStretch()
        return w

    def _page_done(self) -> QWidget:
        w = QWidget(); w.setStyleSheet("background:transparent;")
        lay = QVBoxLayout(w); lay.setContentsMargins(0,0,0,0); lay.setSpacing(10)
        done_title = QLabel("✓  Setup Complete")
        done_title.setStyleSheet(f"color:{C_GREEN};font-size:18px;font-weight:600;")
        done_desc = QLabel("Your hardware profile has been saved.\nThe dashboard is ready to use.")
        done_desc.setStyleSheet(f"color:{C_TEXT2};font-size:12px;")
        done_desc.setWordWrap(True)
        lay.addWidget(done_title); lay.addWidget(done_desc); lay.addSpacing(8)

        self._summary_lbl = QLabel("")
        self._summary_lbl.setStyleSheet(
            f"color:{C_TEXT2};font-size:12px;font-family:monospace;"
            f"background:{C_CARD};border-radius:8px;padding:12px;")
        self._summary_lbl.setWordWrap(True)
        lay.addWidget(self._summary_lbl)
        lay.addStretch()
        return w

    def _on_lang_select(self, item):
        code = item.data(Qt.ItemDataRole.UserRole)
        save_language(code)

    def _update_dots(self, page: int):
        for i, d in enumerate(self._dot_lbls):
            d.setStyleSheet(
                f"color:{C_ACCENT};font-size:10px;" if i == page
                else f"color:{C_TEXT3};font-size:10px;"
            )

    def _go_next(self):
        cur = self._stack.currentIndex()
        if cur == 0:
            # Save language from list
            item = self._lang_list.currentItem()
            if item:
                save_language(item.data(Qt.ItemDataRole.UserRole))
            self._stack.setCurrentIndex(1)
            self._back_btn.setVisible(True)
            self._update_dots(1)
            self._next_btn.setEnabled(False)
            self._next_btn.setText("Detecting…")
            # Run detection in background
            threading.Thread(target=self._run_detection, daemon=True).start()

        elif cur == 1:
            self._stack.setCurrentIndex(2)
            self._update_dots(2)
            self._next_btn.setText("Finish")

        elif cur == 2:
            self.accept()

    def _go_back(self):
        cur = self._stack.currentIndex()
        if cur > 0:
            self._stack.setCurrentIndex(cur - 1)
            self._update_dots(cur - 1)
            if cur - 1 == 0:
                self._back_btn.setVisible(False)
            self._next_btn.setText("Next →")
            self._next_btn.setEnabled(True)

    def _run_detection(self):
        """Called from worker thread."""
        from PyQt6.QtCore import QMetaObject, Q_ARG
        def upd(msg):
            QMetaObject.invokeMethod(self._detect_status, "setText",
                Qt.ConnectionType.QueuedConnection, Q_ARG(str, msg))

        QMetaObject.invokeMethod(self._progress, "show",
            Qt.ConnectionType.QueuedConnection)

        steps = [
            ("Reading DMI info…",           lambda: _dmi("product_name")),
            ("Checking power profiles…",    lambda: Path("/sys/firmware/acpi/platform_profile").exists()),
            ("Checking fan control…",       lambda: FAN_FULLSPEED.exists()),
            ("Checking battery paths…",     lambda: BAT.exists()),
            ("Checking backlight…",         lambda: any(Path("/sys/class/backlight").iterdir()) if Path("/sys/class/backlight").exists() else False),
            ("Checking ThinkPad features…", lambda: Path("/proc/acpi/ibm/fan").exists()),
            ("Checking Yoga hinge…",        lambda: Path("/sys/bus/platform/drivers/lenovo-ymc").exists()),
            ("Checking envycontrol…",       lambda: subprocess.run(["which","envycontrol"],capture_output=True).returncode==0),
            ("Checking legionaura…",        lambda: subprocess.run(["which","legionaura"],capture_output=True).returncode==0),
            ("Building capability map…",    lambda: detect_hardware()),
        ]

        lines = []
        cap = {}
        for msg, fn in steps:
            upd(msg)
            time.sleep(0.15)
            try:
                result = fn()
                if isinstance(result, dict):
                    cap = result
                status = "✓" if result else "—"
                lines.append(f"{status}  {msg.rstrip('…')}")
            except Exception as e:
                lines.append(f"✗  {msg.rstrip('…')}: {e}")

        if not cap:
            cap = detect_hardware()

        save_hardware(cap)
        FIRST_RUN_FLAG.parent.mkdir(parents=True, exist_ok=True)
        FIRST_RUN_FLAG.touch()

        global HW
        HW = cap

        # Build summary
        brand = cap.get("brand","unknown").upper()
        model = cap.get("model","Unknown")
        feats = []
        if cap.get("platform_profile"): feats.append("Power Profiles")
        if cap.get("fan_fullspeed"):    feats.append("Fan Control")
        if cap.get("tp_charge_start"):  feats.append("ThinkPad Charge Thresholds")
        if cap.get("tp_fan_control"):   feats.append("ThinkPad Fan Levels")
        if cap.get("yoga_hinge"):       feats.append("Yoga Hinge Mode")
        if cap.get("legionaura"):       feats.append("RGB Keyboard (LegionAura)")
        if cap.get("envycontrol"):      feats.append("GPU Mode Switching")
        if cap.get("overdrive"):        feats.append("Display Overdrive")
        if cap.get("gsync"):            feats.append("G-Sync")
        if cap.get("nw_backlight"):    feats.append("Brightness Backlight")

        summary = f"Brand: {brand}\nModel: {model}\n\nDetected features:\n"
        summary += "\n".join(f"  ✓  {f}" for f in feats) if feats else "  — No special features detected"

        def finish_up():
            self._progress.hide()
            self._detect_status.setText("\n".join(lines[-4:]))
            self._summary_lbl.setText(summary)
            self._next_btn.setEnabled(True)
            self._next_btn.setText("Next →")
            self._stack.setCurrentIndex(2)
            self._update_dots(2)
            self._next_btn.setText("Finish")

        QMetaObject.invokeMethod(self, "_finish_detection",
            Qt.ConnectionType.QueuedConnection)

    from PyQt6.QtCore import pyqtSlot

    @pyqtSlot()
    def _finish_detection(self):
        cap = HW
        brand = cap.get("brand","unknown").upper()
        model = cap.get("model","Unknown")
        feats = []
        if cap.get("platform_profile"): feats.append("Power Profiles")
        if cap.get("fan_fullspeed"):    feats.append("Fan Control")
        if cap.get("tp_charge_start"):  feats.append("ThinkPad Charge Thresholds")
        if cap.get("tp_fan_control"):   feats.append("ThinkPad Fan Levels (0–7)")
        if cap.get("yoga_hinge"):       feats.append("Yoga Hinge Mode")
        if cap.get("legionaura"):       feats.append("RGB Keyboard")
        if cap.get("envycontrol"):      feats.append("GPU Mode Switching")
        if cap.get("overdrive"):        feats.append("Display Overdrive")
        if cap.get("gsync"):            feats.append("G-Sync")
        if cap.get("nw_backlight"):    feats.append("Brightness Backlight")
        if cap.get("tp_thinklight"):    feats.append("ThinkLight")
        if cap.get("tp_micmute_led"):   feats.append("Mic Mute LED")
        if cap.get("als_sensor"):       feats.append("Ambient Light Sensor")

        summary = f"Brand:  {brand}\nModel:  {model}\n\nAvailable features:\n"
        summary += "\n".join(f"  ✓  {f}" for f in feats) if feats else "  — Standard features only"
        self._summary_lbl.setText(summary)
        self._progress.hide()
        self._next_btn.setEnabled(True)
        self._next_btn.setText("Finish")
        self._update_dots(2)
        self._stack.setCurrentIndex(2)


class ToggleSwitch(QWidget):
    def __init__(self, path=None, on_change=None, parent=None, read_val=None):
        super().__init__(parent)
        self.path = path; self.on_change = on_change
        val = read_val if read_val is not None else (rdsys(path) if path else "0")
        self._checked = val == "1"
        self._cx = 26.0 if self._checked else 6.0
        self.setFixedSize(56, 32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._anim = QPropertyAnimation(self, b"cx", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    @pyqtProperty(float)
    def cx(self): return self._cx
    @cx.setter
    def cx(self, v): self._cx = v; self.update()

    def isChecked(self): return self._checked

    def setChecked(self, val, write=True, notify_title=None, notify_on=None, notify_off=None, silent=False):
        self._checked = val
        self._anim.stop()
        self._anim.setStartValue(self._cx)
        self._anim.setEndValue(26.0 if val else 6.0)
        self._anim.start(); self.update()
        if write and self.path:
            wrsys(self.path, "1" if val else "0")
        if notify_title and not silent:
            body = notify_on if val else notify_off or ""
            send_notif(notify_title, body, "dialog-information")
        if self.on_change and not silent:
            self.on_change(val)

    def mousePressEvent(self, e):
        if self.path and Path(self.path).exists():
            actual = rdsys(self.path, "0") == "1"
            self.setChecked(not actual)
        else:
            self.setChecked(not self._checked)

    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg_color = C_ACCENT if self._checked else C_TEXT3
        p.setBrush(QBrush(QColor(bg_color)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(0, 0, 56, 32, 16, 16)
        p.setBrush(QBrush(QColor("#ffffff")))
        p.drawEllipse(int(self._cx), 6, 20, 20); p.end()


class NotifyToggle(QWidget):
    """ToggleRow that sends a desktop notification on change."""
    def __init__(self, title, desc, path,
                 notif_title=None, notif_on="Enabled", notif_off="Disabled",
                 on_change=None, read_val=None, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:transparent;"); self.setFixedHeight(56)
        self._notif_title = notif_title or title
        self._notif_on    = notif_on
        self._notif_off   = notif_off
        lay = QHBoxLayout(self); lay.setContentsMargins(0,4,0,4)
        col = QVBoxLayout(); col.setSpacing(3)
        t = QLabel(title)
        t.setStyleSheet(f"color:{C_TEXT};font-size:13px;font-weight:500;background:transparent;border:none;")
        d = QLabel(desc)
        d.setStyleSheet(f"color:{C_TEXT2};font-size:12px;background:transparent;border:none;")
        d.setWordWrap(True)
        col.addWidget(t); col.addWidget(d)
        lay.addLayout(col); lay.addStretch()
        self.toggle = ToggleSwitch(path, self._on_toggle, parent=self, read_val=read_val)
        lay.addWidget(self.toggle, alignment=Qt.AlignmentFlag.AlignVCenter)
        self._on_change = on_change

    def _on_toggle(self, val):
        send_notif(self._notif_title,
                   self._notif_on if val else self._notif_off)
        if self._on_change: self._on_change(val)


def _mk_lbl(text: str, color: str = None, size: int = 12, bold: bool = False) -> QLabel:
    """Quick styled QLabel factory."""
    lbl = QLabel(text)
    c = color or C_TEXT2
    w = "600" if bold else "400"
    lbl.setStyleSheet(f"color:{c};font-size:{size}px;font-weight:{w};background:transparent;")
    lbl.setWordWrap(True)
    return lbl

def _mk_lineedit(text: str = "", width: int = 100, placeholder: str = "") -> "QLineEdit":
    from PyQt6.QtWidgets import QLineEdit
    le = QLineEdit(text)
    le.setPlaceholderText(placeholder)
    le.setFixedWidth(width)
    le.setStyleSheet(
        f"QLineEdit{{background:{C_CARD2};color:{C_TEXT};border:none;"
        f"border-radius:8px;padding:8px 12px;font-size:13px;selection-background-color:{C_ACCENT};}}"
    )
    return le

def make_div():
    f = QWidget(); f.setFixedHeight(6); f.setStyleSheet("background:transparent;")
    return f

def make_card(title=""):
    card = QWidget()
    card.setStyleSheet(f"background:{C_CARD};border-radius:12px;")
    card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    lay = QVBoxLayout(card); lay.setContentsMargins(20,16,20,16); lay.setSpacing(12)
    if title:
        t = QLabel(title)
        t.setStyleSheet(f"color:{C_TEXT};font-size:14px;font-weight:600;background:transparent;border:none;")
        lay.addWidget(t)
    return card, lay

def sec_title(text):
    l = QLabel(text)
    l.setStyleSheet(f"color:{C_TEXT};font-size:14px;font-weight:600;background:transparent;border:none;")
    return l

def combo_style():
    return (f"QComboBox{{background:{C_CARD2};color:{C_TEXT};border:none;"
            f"border-radius:8px;padding:8px 14px;font-size:13px;min-width:180px;}}"
            f"QComboBox::drop-down{{border:none;width:24px;}}"
            f"QComboBox QAbstractItemView{{background:{C_CARD2};color:{C_TEXT};"
            f"border:none;selection-background-color:{C_ACCENT};selection-color:#fff;"
            f"padding:4px;}}")


class StatusBadge(QWidget):
    def __init__(self, title, value="—", color=C_TEXT3, tooltip="", parent=None):
        super().__init__(parent)
        self.setMinimumWidth(90)
        self.setFixedHeight(60)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            f"QWidget{{background:{C_CARD2};border-radius:10px;}}"
        )
        lay = QVBoxLayout(self); lay.setContentsMargins(10,8,10,8); lay.setSpacing(2)
        self._t = QLabel(title)
        self._t.setStyleSheet(f"color:{C_TEXT2};font-size:10px;background:transparent;border:none;font-weight:500;")
        self._v = QLabel(value)
        self._v.setStyleSheet(f"color:{color};font-size:13px;font-weight:700;background:transparent;border:none;")
        self._v.setWordWrap(False)
        self._v.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay.addWidget(self._t); lay.addWidget(self._v)
        if tooltip: self.setToolTip(tooltip)

    def set_value(self, v, color=None):
        self._v.setText(v)
        if color:
            self._v.setStyleSheet(
                f"color:{color};font-size:12px;font-weight:600;background:transparent;border:none;"
            )


class AIBadge(StatusBadge):
    """StatusBadge with an inline toggle switch — identical size/style to peers."""
    toggled = None  # callback(bool)

    def __init__(self, on_change=None, parent=None):
        super().__init__("L1 AI Engine", "OFF", C_TEXT3,
                         "Lenovo L1 AI Engine\nOn Linux: adjusts EPP for performance.\nToggle is manual — never auto-changed by profile switching.",
                         parent)
        self.toggled = on_change
        # Replace the value label row with value + toggle side by side
        lay = self.layout()
        # Remove old value label
        lay.removeWidget(self._v)
        self._v.setParent(None)
        # New row: value text + toggle
        row = QHBoxLayout(); row.setContentsMargins(0,0,0,0); row.setSpacing(4)
        self._v = QLabel("OFF")
        self._v.setStyleSheet(f"color:{C_TEXT3};font-size:12px;font-weight:600;background:transparent;border:none;")
        self._v.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._tog = ToggleSwitch(path=None, on_change=self._handle_toggle, read_val="0")
        self._tog.setFixedSize(36, 20)
        row.addWidget(self._v); row.addWidget(self._tog)
        lay.addLayout(row)

    def _handle_toggle(self, val):
        col = C_GREEN if val else C_TEXT3
        self._v.setText("ON" if val else "OFF")
        self._v.setStyleSheet(f"color:{col};font-size:12px;font-weight:600;background:transparent;border:none;")
        if self.toggled:
            self.toggled(val)

    def set_state(self, is_on: bool, silent: bool = False):
        """Update visual state WITHOUT triggering the callback."""
        col = C_GREEN if is_on else C_TEXT3
        self._v.setText("ON" if is_on else "OFF")
        self._v.setStyleSheet(f"color:{col};font-size:12px;font-weight:600;background:transparent;border:none;")
        self._tog._checked = is_on
        self._tog._cx = 22.0 if is_on else 4.0
        self._tog.update()


class ProfileBtn(QPushButton):
    def __init__(self, profile, parent=None):
        super().__init__(parent)
        self.profile = profile
        self.setCheckable(True)
        self.setFixedHeight(72)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        color  = PROFILE_COLORS[profile]
        icon   = PROFILE_ICONS[profile]
        label  = PROFILE_LABELS[profile]
        desc   = PROFILE_DESCS[profile].split(" · ")[0]   # first part only e.g. "15W"
        # Unchecked: dark card
        # Checked: colored border + subtle color background
        import re as _re
        r = int(_re.search(r'#(..)', color).group(1), 16) if '#' in color else 255
        self.setStyleSheet(
            f"QPushButton{{"
            f"  background:{C_CARD2};color:{C_TEXT2};"
            f"  border:none;border-radius:10px;"
            f"  font-size:12px;text-align:center;padding:4px 2px;"
            f"}}"
            f"QPushButton:checked{{"
            f"  background:rgba({int(color[1:3],16)},{int(color[3:5],16)},{int(color[5:7],16)},30);"
            f"  color:{color};border:none;border-radius:10px;"
            f"}}"
            f"QPushButton:hover:!checked{{"
            f"  background:{C_HOVER};color:{C_TEXT};"
            f"}}"
        )
        # Layout inside button: icon on top, label below, watt hint at bottom
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 6, 4, 6)
        lay.setSpacing(1)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_icon = QLabel(icon)
        lbl_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_icon.setStyleSheet("background:transparent;font-size:16px;")
        lbl_name = QLabel(label)
        lbl_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_name.setStyleSheet(f"background:transparent;font-size:12px;font-weight:600;color:{color};")
        lbl_desc = QLabel(desc)
        lbl_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_desc.setStyleSheet(f"background:transparent;font-size:12px;color:{C_TEXT3};")
        lay.addWidget(lbl_icon)
        lay.addWidget(lbl_name)
        lay.addWidget(lbl_desc)


class SidebarBtn(QPushButton):
    def __init__(self, icon_char, label, parent=None):
        super().__init__(parent); self.setCheckable(True)
        self.setFixedSize(204, 44)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.icon_char = icon_char; self.label = label

        self.setLayout(QHBoxLayout())
        self.layout().setContentsMargins(16, 0, 16, 0)
        self.layout().setSpacing(12)
        self.layout().setAlignment(Qt.AlignmentFlag.AlignLeft)

        self._icon_lbl = QLabel(icon_char)
        self._icon_lbl.setStyleSheet(f"font-size:18px;background:transparent;")
        self._icon_lbl.setFixedSize(20, 20)
        self._icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._text_lbl = QLabel(label)
        self._text_lbl.setStyleSheet(f"font-size:13px;background:transparent;font-weight:500;")

        self.layout().addWidget(self._icon_lbl)
        self.layout().addWidget(self._text_lbl)
        self.layout().addStretch()

        self.toggled.connect(self._update_style)
        self._update_style(self.isChecked())

    def _update_style(self, checked):
        if checked:
            bg = C_ACTIVE; fg = C_ACCENT; tw = 600
            il_color = C_ACCENT; tl_color = C_ACCENT
        else:
            bg = "transparent"; fg = C_TEXT2; tw = 500
            il_color = C_TEXT3; tl_color = C_TEXT2
        self.setStyleSheet(
            f"QPushButton{{background:{bg};border:none;color:{fg};"
            f"border-radius:8px;}}"
            f"QPushButton:hover{{background:{C_HOVER};}}"
        )
        self._icon_lbl.setStyleSheet(f"color:{il_color};font-size:18px;background:transparent;")
        self._text_lbl.setStyleSheet(f"color:{tl_color};font-size:13px;background:transparent;font-weight:{tw};")


def scrollable(widget_factory):
    """Wrap a QVBoxLayout-based page in a scroll area."""
    outer = QWidget()
    outer.setStyleSheet(f"background:{C_BG};")
    scroll = QScrollArea(outer)
    scroll.setWidgetResizable(True)
    scroll.setStyleSheet("border:none;background:transparent;")
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    inner = QWidget(); inner.setStyleSheet(f"background:{C_BG};")
    root = QVBoxLayout(inner); root.setContentsMargins(24,24,24,24); root.setSpacing(12)
    lay = QVBoxLayout(outer); lay.setContentsMargins(0,0,0,0); lay.addWidget(scroll)
    scroll.setWidget(inner)
    widget_factory(root)
    root.addStretch()
    return outer

# ══════════════════════════════════════════════════════════════════════════════
# HOME PAGE
# ══════════════════════════════════════════════════════════════════════════════
class HomePage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        lbl = QLabel("HomePage")
        lbl.setStyleSheet("color:#888;font-size:16px;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl)
# ══════════════════════════════════════════════════════════════════════════════
# BATTERY PAGE
# ══════════════════════════════════════════════════════════════════════════════
class BatteryPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        lbl = QLabel("BatteryPage")
        lbl.setStyleSheet("color:#888;font-size:16px;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl)
# ══════════════════════════════════════════════════════════════════════════════
# PERFORMANCE PAGE
# ══════════════════════════════════════════════════════════════════════════════
class PerformancePage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        lbl = QLabel("PerformancePage")
        lbl.setStyleSheet("color:#888;font-size:16px;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl)
# ══════════════════════════════════════════════════════════════════════════════
# DISPLAY PAGE
# ══════════════════════════════════════════════════════════════════════════════
class DisplayPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        lbl = QLabel("DisplayPage")
        lbl.setStyleSheet("color:#888;font-size:16px;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl)
# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
# KEYBOARD PAGE — via legionaura
# ══════════════════════════════════════════════════════════════════════════════
class KeyboardPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        lbl = QLabel("KeyboardPage")
        lbl.setStyleSheet("color:#888;font-size:16px;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl)
class SystemPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        lbl = QLabel("SystemPage")
        lbl.setStyleSheet("color:#888;font-size:16px;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl)
# ══════════════════════════════════════════════════════════════════════════════
# OVERCLOCK PAGE
# ══════════════════════════════════════════════════════════════════════════════
class OverclockPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        lbl = QLabel("OverclockPage")
        lbl.setStyleSheet("color:#888;font-size:16px;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl)
# ══════════════════════════════════════════════════════════════════════════════
# FAN CURVE PAGE
# ══════════════════════════════════════════════════════════════════════════════
class FanWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        lbl = QLabel("FanWidget")
        lbl.setStyleSheet("color:#888;font-size:16px;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl)
class FanPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        lbl = QLabel("FanPage")
        lbl.setStyleSheet("color:#888;font-size:16px;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl)
# ══════════════════════════════════════════════════════════════════════════════
class ActionsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        lbl = QLabel("ActionsPage")
        lbl.setStyleSheet("color:#888;font-size:16px;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl)
# ══════════════════════════════════════════════════════════════════════════════
        color = C_GREEN if ok else C_ORANGE
        self._status.setStyleSheet(
            f"color:{color};font-size:12px;font-weight:600;background:transparent;")
        self._status.setText(msg)

    def _build(self):
        scroll = QScrollArea(self); scroll.setWidgetResizable(True)
        scroll.setStyleSheet("border:none;background:transparent;")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget(); inner.setStyleSheet(f"background:{C_BG};")
        root = QVBoxLayout(inner); root.setContentsMargins(24,24,24,24); root.setSpacing(12)
        lay = QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.addWidget(scroll)
        scroll.setWidget(inner)

        # ── Driver status banner ──────────────────────────────────────────────
        info = _fan_hwmon_info()
        fc, fl = make_card("Fan Control")
        if info["found"]:
            has_pwm = info["pwm1"] or info["pwm2"]
            has_en  = info["pwm1_enable"] or info["pwm2_enable"]
            fs_ok   = FAN_FULLSPEED.exists()
            lines = [
                f"✓  legion_hwmon found: {info['path']}",
                f"   PWM files: {'pwm1 pwm2' if has_pwm else '— not available (read-only RPM only)'}",
                f"   PWM enable: {'pwm1_enable pwm2_enable' if has_en else '— not available'}",
                f"   fan_fullspeed: {'✓' if fs_ok else '✗  not found'} {FAN_FULLSPEED}",
            ]
            if not has_pwm:
                lines.append("")
                lines.append("⚠  Manual speed via PWM not available on this driver version.")
                lines.append("   Auto / Full Speed still work. Presets use Full Speed toggle.")
            status_lbl = QLabel("\n".join(lines))
            status_lbl.setStyleSheet(
                f"color:{C_TEXT2};font-size:10px;font-family:monospace;background:transparent;")
        else:
            status_lbl = QLabel(
                "⚠  legion_hwmon not found.\n"
                "Make sure lenovo_legion_laptop module is loaded:\n"
                "sudo modprobe lenovo_legion_laptop")
            status_lbl.setStyleSheet(f"color:{C_ORANGE};font-size:12px;background:transparent;")
        status_lbl.setWordWrap(True)
        fl.addWidget(status_lbl)
        root.addWidget(fc)

        # ── Live RPM ──────────────────────────────────────────────────────────
        rc, rl = make_card("Live Fan Speed")
        rpm_row = QHBoxLayout(); rpm_row.setSpacing(32)
        for attr, label, color in [
            ("cpu_rpm_lbl","CPU Fan",C_BLUE),
            ("gpu_rpm_lbl","GPU Fan",C_RED),
        ]:
            col = QVBoxLayout(); col.setSpacing(4)
            icon = QLabel("🌀"); icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            icon.setStyleSheet("font-size:22px;background:transparent;")
            lbl = QLabel("— RPM"); lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(f"color:{color};font-size:20px;font-weight:600;background:transparent;")
            name = QLabel(label); name.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name.setStyleSheet(f"color:{C_TEXT2};font-size:12px;background:transparent;")
            setattr(self, attr, lbl)
            col.addWidget(icon); col.addWidget(lbl); col.addWidget(name)
            rpm_row.addLayout(col)
        rl.addLayout(rpm_row)
        self.fan_mode_badge = QLabel("Mode: Auto")
        self.fan_mode_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.fan_mode_badge.setStyleSheet(f"color:{C_TEXT2};font-size:12px;background:transparent;")
        rl.addWidget(self.fan_mode_badge)
        root.addWidget(rc)

        # ── Mode selector ─────────────────────────────────────────────────────
        mc, ml = make_card("Fan Control Mode")
        mode_row = QHBoxLayout(); mode_row.setSpacing(8)
        self._mode_btns = {}
        for mode_name, mode_key, color in [
            ("Auto / Dynamic", "auto",   C_GREEN),
            ("Manual Speed",   "manual", C_ORANGE),
            ("Full Speed",     "full",   C_RED),
        ]:
            btn = QPushButton(mode_name)
            btn.setCheckable(True); btn.setFixedHeight(36)
            btn.setChecked(self._cfg.get("mode","auto") == mode_key)
            btn.setStyleSheet(
                f"QPushButton{{background:{C_CARD2};color:{C_TEXT2};"
                f"border:1px solid {C_BORDER};border-radius:6px;"
                f"font-size:12px;font-weight:600;padding:0 8px;}}"
                f"QPushButton:checked{{background:transparent;color:{color};"
                f"border:2px solid {color};}}"
                f"QPushButton:hover:!checked{{border:1px solid #555;color:{C_TEXT};}}"
            )
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda chk, k=mode_key: self._set_mode(k))
            self._mode_btns[mode_key] = btn; mode_row.addWidget(btn)
        ml.addLayout(mode_row)
        self._mode_desc = QLabel(self._mode_hint(self._cfg.get("mode","auto")))
        self._mode_desc.setStyleSheet(f"color:{C_TEXT2};font-size:12px;background:transparent;")
        self._mode_desc.setWordWrap(True)
        ml.addWidget(self._mode_desc)
        root.addWidget(mc)

        # ── Presets ───────────────────────────────────────────────────────────
        pc, pl = make_card("Fan Presets")
        pl.addWidget(_mk_lbl(
            "Quick presets. If manual PWM is available, sets exact speed.\n"
            "Otherwise maps to Auto or Full Speed.", C_TEXT2, size=12))
        preset_wrap = QWidget()
        preset_wrap.setStyleSheet("background:transparent;")
        preset_lay = QVBoxLayout(preset_wrap)
        preset_lay.setContentsMargins(0, 8, 0, 0)
        preset_lay.setSpacing(8)
        preset_list = list(FAN_PRESETS.items())
        for row_i in range(0, len(preset_list), 3):
            row = QHBoxLayout()
            row.setSpacing(8)
            for col_i in range(3):
                idx = row_i + col_i
                if idx >= len(preset_list):
                    row.addStretch()
                    continue
                pname, (cpu_pct, gpu_pct) = preset_list[idx]
                color = [C_BLUE, C_GREEN, C_ORANGE, C_RED, "#ff0000"][idx % 5]
                btn = QPushButton(f"{pname}  —  {cpu_pct}% CPU / {gpu_pct}% GPU")
                btn.setFixedHeight(42)
                btn.setStyleSheet(
                    f"QPushButton{{background:{C_CARD2};color:{C_TEXT};"
                    f"border:1px solid {C_BORDER};border-radius:8px;font-size:12px;font-weight:500;"
                    f"border-left:3px solid {color};padding-left:12px;}}"
                    f"QPushButton:hover{{background:{color}22;border-color:{color};}}"
                    f"QPushButton:pressed{{background:{color}44;}}"
                )
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.clicked.connect(lambda chk, pn=pname, cp=cpu_pct, gp=gpu_pct:
                                    self._apply_preset(pn, cp, gp))
                row.addWidget(btn, 1)
            preset_lay.addLayout(row)
        pl.addWidget(preset_wrap)
        root.addWidget(pc)

        # ── Manual sliders ────────────────────────────────────────────────────
        self._manual_card, ml2 = make_card("Manual Fan Speed")
        ml2.addWidget(_mk_lbl(
            "Set exact fan speed. Requires writable PWM files in legion_hwmon.", C_TEXT2, size=11))

        def _slider_row(label, color, default):
            row = QHBoxLayout(); row.setSpacing(12)
            lb = QLabel(label); lb.setFixedWidth(70)
            lb.setStyleSheet(f"color:{color};font-size:12px;font-weight:600;background:transparent;")
            sl = QSlider(Qt.Orientation.Horizontal)
            sl.setRange(0,100); sl.setValue(default)
            sl.setStyleSheet(
                f"QSlider::groove:horizontal{{background:{C_BORDER};height:8px;border-radius:4px;}}"
                f"QSlider::handle:horizontal{{background:{color};width:18px;height:18px;"
                f"border-radius:9px;margin:-5px 0;}}"
                f"QSlider::sub-page:horizontal{{background:{color};border-radius:4px;}}"
            )
            vl = QLabel(f"{default}%"); vl.setFixedWidth(40)
            vl.setStyleSheet(f"color:{color};font-size:12px;font-weight:600;background:transparent;")
            sl.valueChanged.connect(lambda v, l=vl: l.setText(f"{v}%"))
            row.addWidget(lb); row.addWidget(sl); row.addWidget(vl)
            return row, sl

        cpu_row, self.cpu_fan_sl = _slider_row("CPU Fan", C_BLUE,  self._cfg.get("cpu_pct",50))
        gpu_row, self.gpu_fan_sl = _slider_row("GPU Fan", C_RED,   self._cfg.get("gpu_pct",50))
        ml2.addLayout(cpu_row); ml2.addLayout(gpu_row)

        apply_btn = QPushButton("Apply Manual Speed")
        apply_btn.setFixedHeight(34)
        apply_btn.setStyleSheet(
            f"background:{C_ORANGE};color:#000;font-weight:600;"
            f"border:none;border-radius:6px;font-size:12px;padding:0 16px;")
        apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_btn.clicked.connect(self._apply_manual)
        btn_rl = QHBoxLayout(); btn_rl.addWidget(apply_btn); btn_rl.addStretch()
        ml2.addLayout(btn_rl)
        root.addWidget(self._manual_card)

        # Status label
        self._fan_status = QLabel("")
        self._fan_status.setStyleSheet(f"color:{C_GREEN};font-size:12px;background:transparent;")
        root.addWidget(self._fan_status)
        root.addStretch()

        self._update_manual_visibility(self._cfg.get("mode","auto"))
        self._refresh_rpm()

    def _mode_hint(self, mode: str) -> str:
        return {
            "auto":   "Firmware controls fans via thermal curves. Recommended for daily use.",
            "manual": "Set exact fan speed. Uses PWM files in legion_hwmon.",
            "full":   "Both fans locked to 100% — loudest, maximum cooling.",
        }.get(mode, "")

    def _set_mode(self, mode: str):
        for k, b in self._mode_btns.items():
            b.setChecked(k == mode)
        self._mode_desc.setText(self._mode_hint(mode))
        
        # Show/hide fan curve editor based on mode
        # Editor visible for manual or full mode (NOT auto)
        if hasattr(self, '_fancurve_editor'):
            self._fancurve_editor.setVisible(mode in ("manual", "full"))
        
        self._cfg["mode"] = mode
        save_fan_config(self._cfg)
        self._update_manual_visibility(mode)
        self._on_fan_result(False, f"⏳  Applying {mode} mode…")
        def _do():
            if mode == "auto":
                ok, msg = _write_fan_auto()
                self._emit(ok, "✓  Auto fan control active" if ok else f"✗  {msg}")
            elif mode == "full":
                ok, msg = _write_fan_fullspeed(True)
                self._emit(ok, "✓  Full speed active" if ok else f"✗  {msg}")
            elif mode == "manual":
                self._emit(True, "↑  Set speed with sliders above → Apply")
        threading.Thread(target=_do, daemon=True).start()

    def _update_manual_visibility(self, mode: str):
        self._manual_card.setVisible(mode == "manual")

    def _apply_preset(self, name: str, cpu_pct: int, gpu_pct: int):
        self.cpu_fan_sl.setValue(cpu_pct)
        self.gpu_fan_sl.setValue(gpu_pct)
        for k, b in self._mode_btns.items():
            b.setChecked(k == "manual")
        self._update_manual_visibility("manual")
        self._cfg.update({"mode":"manual","cpu_pct":cpu_pct,"gpu_pct":gpu_pct,"preset":name})
        save_fan_config(self._cfg)
        self._on_fan_result(False, f"⏳  Applying {name}…")
        def _do():
            ok, msg = _write_fan_pwm(cpu_pct, gpu_pct)
            if ok:
                self._emit(True, f"✓  {name} — CPU {cpu_pct}%  GPU {gpu_pct}%")
            else:
                if cpu_pct >= 90:
                    ok2, msg2 = _write_fan_fullspeed(True)
                    self._emit(ok2, f"✓  {name} (full speed)" if ok2 else f"✗  {msg2}")
                else:
                    ok2, msg2 = _write_fan_auto()
                    self._emit(ok2,
                        f"✓  {name} (auto — PWM not available)" if ok2 else f"✗  {msg2}")
        threading.Thread(target=_do, daemon=True).start()

    def _apply_manual(self):
        cpu_pct = self.cpu_fan_sl.value()
        gpu_pct = self.gpu_fan_sl.value()
        self._cfg.update({"mode":"manual","cpu_pct":cpu_pct,"gpu_pct":gpu_pct})
        save_fan_config(self._cfg)
        self._on_fan_result(False, f"⏳  Applying CPU {cpu_pct}%  GPU {gpu_pct}%…")
        def _do():
            ok, msg = _write_fan_pwm(cpu_pct, gpu_pct)
            self._emit(ok, f"✓  CPU {cpu_pct}%  GPU {gpu_pct}%" if ok else f"✗  {msg}")
        threading.Thread(target=_do, daemon=True).start()

    def _refresh_rpm(self):
        rpm1, rpm2 = get_fan_rpm()
        self.cpu_rpm_lbl.setText(f"{rpm1:,}" if rpm1 > 0 else "—")
        self.gpu_rpm_lbl.setText(f"{rpm2:,}" if rpm2 > 0 else "—")
        mode = self._cfg.get("mode","auto")
        mode_labels = {"auto":"Auto","manual":"Manual","full":"Full Speed"}
        self.fan_mode_badge.setText(f"Mode: {mode_labels.get(mode, mode)}")
        for lbl, rpm, col in [(self.cpu_rpm_lbl,rpm1,C_BLUE),(self.gpu_rpm_lbl,rpm2,C_RED)]:
            c = C_RED if rpm>5000 else C_ORANGE if rpm>2500 else col
            lbl.setStyleSheet(
                f"color:{c};font-size:20px;font-weight:600;background:transparent;")

    def refresh(self, d=None):
        self._refresh_rpm()
# ══════════════════════════════════════════════════════════════════════════════
class ActionsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background:{C_BG};")
        self._actions = load_actions()
        self._build()

    def _build(self):
        scroll = QScrollArea(self); scroll.setWidgetResizable(True)
        scroll.setStyleSheet("border:none;background:transparent;")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget(); inner.setStyleSheet(f"background:{C_BG};")
        root = QVBoxLayout(inner); root.setContentsMargins(24,24,24,24); root.setSpacing(12)
        lay = QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.addWidget(scroll)
        scroll.setWidget(inner)

        ac, al = make_card("Automatic Power Mode Switching")
        adesc = QLabel(
            "Automatically switch power profile when AC adapter is plugged or unplugged. "
            "The background sampler applies changes immediately — no separate daemon needed."
        )
        adesc.setWordWrap(True)
        adesc.setStyleSheet(f"color:{C_TEXT2};font-size:12px;background:transparent;")
        al.addWidget(adesc); al.addWidget(make_div())

        sw = QHBoxLayout()
        swc = QVBoxLayout(); swc.setSpacing(2)
        st = QLabel("Enable Auto Switching")
        st.setStyleSheet(f"color:{C_TEXT};font-size:13px;font-weight:600;background:transparent;")
        sd = QLabel("Automatically change profile when charger is plugged/unplugged.")
        sd.setStyleSheet(f"color:{C_TEXT2};font-size:12px;background:transparent;")
        swc.addWidget(st); swc.addWidget(sd)
        sw.addLayout(swc); sw.addStretch()
        self.auto_toggle = ToggleSwitch(
            path=None, on_change=self._on_auto,
            read_val="1" if self._actions.get("auto_switch") else "0"
        )
        sw.addWidget(self.auto_toggle, alignment=Qt.AlignmentFlag.AlignVCenter)
        al.addLayout(sw); al.addWidget(make_div())

        for attr, label, key in [
            ("ac_combo",  "On AC Connect  →", "on_ac"),
            ("bat_combo", "On Battery      →", "on_battery"),
        ]:
            row = QHBoxLayout(); row.setSpacing(16)
            lbl = QLabel(label); lbl.setFixedWidth(180)
            lbl.setStyleSheet(f"color:{C_TEXT};font-size:13px;background:transparent;")
            row.addWidget(lbl)
            combo = QComboBox(); combo.setStyleSheet(combo_style())
            cur = self._actions.get(key,"balanced")
            for p in PROFILES: combo.addItem(PROFILE_LABELS[p], p)
            if cur in PROFILES: combo.setCurrentIndex(PROFILES.index(cur))
            combo.currentIndexChanged.connect(self._save)
            setattr(self, attr, combo); row.addWidget(combo); row.addStretch()
            al.addLayout(row)

        test_row = QHBoxLayout()
        test_btn = QPushButton("Test Now — Apply correct profile")
        test_btn.setStyleSheet(f"background:{C_CARD2};color:{C_TEXT};border:1px solid {C_BORDER};"
                               f"border-radius:6px;padding:8px 16px;font-size:12px;")
        test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        test_btn.clicked.connect(self._test_now)
        test_row.addWidget(test_btn); test_row.addStretch()
        al.addLayout(test_row)

        self.save_lbl = QLabel("")
        self.save_lbl.setStyleSheet(f"color:{C_GREEN};font-size:12px;background:transparent;")
        al.addWidget(self.save_lbl); root.addWidget(ac)

        cs, csl = make_card("Current Status")
        self.ac_status   = InfoRow("Power Source","—"); csl.addWidget(self.ac_status)
        self.prof_status = InfoRow("Active Profile","—"); csl.addWidget(self.prof_status)
        self.auto_status = InfoRow("Auto Switch","—"); csl.addWidget(self.auto_status)
        root.addWidget(cs)

        nc, nl = make_card("ℹ️  How It Works")
        note = QLabel(
            "The background thread checks AC state every second. "
            "When power source changes and Auto Switch is ON, the profile is applied immediately "
            "and a desktop notification is shown.\n\n"
            "Config: ~/.config/legion-toolkit/actions.json"
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{C_TEXT2};font-size:12px;background:transparent;")
        nl.addWidget(note); root.addWidget(nc)
        root.addStretch()

    def _on_auto(self, val):
        self._actions["auto_switch"] = val; self._save_data()

    def _save(self):
        self._actions["on_ac"]       = self.ac_combo.currentData()
        self._actions["on_battery"]  = self.bat_combo.currentData()
        self._actions["auto_switch"] = self.auto_toggle.isChecked()
        self._save_data()

    def _save_data(self):
        save_actions(self._actions)
        self.save_lbl.setText("✓ Saved")
        QTimer.singleShot(2000, lambda: self.save_lbl.setText(""))

    def _test_now(self):
        apply_actions_now()
        self.save_lbl.setText("✓ Profile applied")
        QTimer.singleShot(2000, lambda: self.save_lbl.setText(""))

    def refresh(self, d=None):
        if d:
            ac      = d.get("ac", False)
            profile = d.get("profile", "balanced")
        else:
            ac      = get_ac_connected()
            profile = _read_powermode()
        self.ac_status.set_value("AC Adapter" if ac else "Battery")
        self.prof_status.set_value(PROFILE_LABELS.get(profile, "—"))
        self.auto_status.set_value(
            "✓ Active" if self._actions.get("auto_switch") else "✗ Disabled"
        )

# ══════════════════════════════════════════════════════════════════════════════
# ABOUT PAGE
# ══════════════════════════════════════════════════════════════════════════════
class AboutPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        lbl = QLabel("AboutPage")
        lbl.setStyleSheet("color:#888;font-size:16px;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl)
# ══════════════════════════════════════════════════════════════════════════════
# MAIN WINDOW
# ══════════════════════════════════════════════════════════════════════════════
_sampler = None   # created in main() after QApplication exists

class LegionDashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Legion Linux Toolkit")
        self.setWindowIcon(_legion_icon())
        self.setMinimumSize(1060, 680); self.resize(1160, 740)
        self._build()
        global _sampler
        _sampler = DataSampler()
        _sampler.data_ready.connect(self._on_data)
        _sampler.start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(2000)
        # Start Fn+Space watcher
        self._start_fnspace_watcher()

    def _build(self):
        self.setStyleSheet(
            f"QMainWindow{{background:{C_BG};}}"
            f"QToolTip{{background:{C_CARD2};color:{C_TEXT};border:1px solid {C_BORDER};"
            f"padding:8px;font-size:12px;border-radius:6px;}}"
            f"QScrollBar:vertical{{background:{C_BG};width:8px;border-radius:4px;}}"
            f"QScrollBar::handle:vertical{{background:{C_TEXT3};border-radius:4px;min-height:40px;}}"
            f"QScrollBar::handle:vertical:hover{{background:{C_TEXT2};}}"
            f"QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}"
            f"QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{{background:transparent;}}"
            f"QSlider::groove:horizontal{{background:{C_BORDER};height:6px;border-radius:3px;}}"
            f"QSlider::sub-page:horizontal{{background:{C_ACCENT};border-radius:3px;}}"
            f"QSlider::handle:horizontal{{background:{C_ACCENT};width:18px;height:18px;"
            f"border-radius:9px;margin:-6px 0;}}"
            f"QSlider::handle:horizontal:hover{{background:{C_TEXT};}}"
            f"QPushButton{{background:{C_CARD2};color:{C_TEXT};border:1px solid {C_BORDER};"
            f"border-radius:8px;padding:8px 18px;font-size:13px;font-weight:500;}}"
            f"QPushButton:hover{{background:{C_ACCENT};color:#fff;border-color:{C_ACCENT};}}"
            f"QComboBox{{background:{C_CARD2};color:{C_TEXT};border:1px solid {C_BORDER};"
            f"border-radius:8px;padding:8px 14px;font-size:13px;}}"
            f"QComboBox::drop-down{{border:none;width:24px;}}"
            f"QComboBox QAbstractItemView{{background:{C_CARD2};color:{C_TEXT};"
            f"border:1px solid {C_BORDER};selection-background-color:{C_ACCENT};selection-color:#fff;"
            f"padding:4px;}}"
            f"QSpinBox,QDoubleSpinBox{{background:{C_CARD2};color:{C_TEXT};"
            f"border:1px solid {C_BORDER};border-radius:8px;padding:6px 10px;font-size:13px;}}"
            f"QLineEdit{{background:{C_CARD2};color:{C_TEXT};border:1px solid {C_BORDER};"
            f"border-radius:8px;padding:8px 12px;font-size:13px;selection-background-color:{C_ACCENT};}}"
        )
        rw = QWidget(); self.setCentralWidget(rw)
        main = QHBoxLayout(rw); main.setContentsMargins(0,0,0,0); main.setSpacing(0)

        # Sidebar — wider, LLT-style
        sb = QWidget(); sb.setFixedWidth(220)
        sb.setStyleSheet(f"background:{C_SIDEBAR};")
        sbl = QVBoxLayout(sb); sbl.setContentsMargins(0,0,0,0); sbl.setSpacing(0)

        # Top bar with logo and title
        top_logo = QWidget()
        top_logo.setFixedHeight(64)
        top_logo.setStyleSheet(f"background:{C_SIDEBAR};")
        top_logo_lay = QHBoxLayout(top_logo)
        top_logo_lay.setContentsMargins(16,10,16,10)
        top_logo_lay.setSpacing(10)

        import base64 as _b64
        from PyQt6.QtGui import QPixmap as _QP2
        _pm2 = _QP2(); _pm2.loadFromData(_b64.b64decode(_LEGION_ICON_B64))
        logo_lbl = QLabel()
        logo_lbl.setPixmap(_pm2.scaled(28, 34, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation))
        logo_lbl.setStyleSheet("background:transparent;")
        logo_lbl.setFixedSize(32, 38)

        title_lbl = QLabel("Legion Toolkit")
        title_lbl.setStyleSheet(f"color:{C_TEXT};font-size:15px;font-weight:700;background:transparent;")

        top_logo_lay.addWidget(logo_lbl, alignment=Qt.AlignmentFlag.AlignVCenter)
        top_logo_lay.addWidget(title_lbl, alignment=Qt.AlignmentFlag.AlignVCenter)
        top_logo_lay.addStretch()
        sbl.addWidget(top_logo)

        # Nav buttons
        self.nav_btns = []
        nav = [("🏠","Home"),("🔋","Battery"),("⚡","Performance"),
               ("🖥️","Display"),("⌨️","Keyboard"),("⚙️","System"),
               ("🚀","Overclock"),("🌀","Fan Control"),("🎯","Actions"),("ℹ️","About")]
        nav_area = QWidget()
        nav_area.setStyleSheet(f"background:{C_SIDEBAR};")
        nav_area_lay = QVBoxLayout(nav_area)
        nav_area_lay.setContentsMargins(8,8,8,8)
        nav_area_lay.setSpacing(4)

        for icon, label in nav:
            btn = SidebarBtn(icon, label)
            btn.clicked.connect(lambda chk, i=len(self.nav_btns): self._switch(i))
            self.nav_btns.append(btn)
            nav_area_lay.addWidget(btn)
        nav_area_lay.addStretch()
        sbl.addWidget(nav_area)

        # Bottom section with theme toggle
        bottom_area = QWidget()
        bottom_area.setStyleSheet(f"background:{C_SIDEBAR};")
        bottom_lay = QVBoxLayout(bottom_area)
        bottom_lay.setContentsMargins(8,8,8,8)
        bottom_lay.setSpacing(4)
        bottom_lay.addStretch()
        sbl.addWidget(bottom_area)
        main.addWidget(sb)

        # Right side
        right = QVBoxLayout(); right.setContentsMargins(0,0,0,0); right.setSpacing(0)
        topbar = QWidget(); topbar.setFixedHeight(60)
        topbar.setStyleSheet(
            f"background:{C_BG};"
        )
        tbl = QHBoxLayout(topbar); tbl.setContentsMargins(28,0,28,0); tbl.setSpacing(12)

        # Page title
        self.page_title = QLabel("Home")
        self.page_title.setStyleSheet(
            f"color:{C_TEXT};font-size:20px;font-weight:700;letter-spacing:0px;")
        tbl.addWidget(self.page_title)
        tbl.addStretch()

        # Hidden badge kept for _refresh_badge compat — not shown
        self.badge = QLabel(""); self.badge.hide()
        self.ac_ind = QLabel(""); self.ac_ind.hide()
        self._refresh_badge(_read_powermode())
        right.addWidget(topbar)

        self.stack = QStackedWidget()
        self.stack.setStyleSheet(f"background:{C_BG};")
        self.home_page = HomePage()
        self.home_page._page_request_cb = self._switch
        self.pages = [
            self.home_page, BatteryPage(), PerformancePage(),
            DisplayPage(), KeyboardPage(), SystemPage(),
            OverclockPage(), FanPage(), ActionsPage(), AboutPage()
        ]
        self.home_page._sync_battery_cb = self.pages[1].sync_charging
        self.pages[1]._sync_home_cb = self._sync_bat_combo
        for pg in self.pages: self.stack.addWidget(pg)
        right.addWidget(self.stack); main.addLayout(right)
        self._switch(0)

    def _start_fnspace_watcher(self):
        """
        Watch kbd_backlight brightness for changes — fires when Fn+Space is pressed.
        Fn+Space cycles brightness 0→1→2→0, we intercept and also cycle RGB effect.
        """
        from PyQt6.QtCore import QMetaObject
        self._fnspace_signal = pyqtSignal()

        kbd_path = KBD_BACKLIGHT_PATH
        if kbd_path is None or not Path(str(kbd_path)).exists():
            return  # No backlight path — skip

        def _watch():
            last = None
            while True:
                try:
                    val = Path(str(kbd_path)).read_text().strip()
                    if last is not None and val != last:
                        # Brightness changed — Fn+Space was pressed
                        QMetaObject.invokeMethod(
                            self, "_on_fnspace",
                            Qt.ConnectionType.QueuedConnection
                        )
                    last = val
                except: pass
                time.sleep(0.15)

        threading.Thread(target=_watch, daemon=True).start()

    from PyQt6.QtCore import pyqtSlot

    @pyqtSlot()
    def _on_fnspace(self):
        """Called on main thread when Fn+Space is detected."""
        # Cycle the keyboard effect
        kb_page = self.pages[4]   # KeyboardPage is index 4
        if hasattr(kb_page, "cycle_effect"):
            kb_page.cycle_effect()
        # If keyboard page is not visible, show a brief notification
        if self.stack.currentIndex() != 4:
            cur = getattr(kb_page, "_current_effect", "Static")
            send_notif("Keyboard RGB", f"Effect → {cur}", "input-keyboard")

    def _sync_bat_combo(self, idx: int):
        """Sync Home page battery combo when Battery page toggle changes."""
        self.home_page.bat_combo.blockSignals(True)
        self.home_page.bat_combo.setCurrentIndex(idx)
        self.home_page.bat_combo.blockSignals(False)

    def _switch(self, idx):
        self.stack.setCurrentIndex(idx)
        titles = ["Home","Battery","Performance","Display","Keyboard",
                  "System","Overclock","Fan","Actions","About"]
        self.page_title.setText(titles[idx])
        for i, btn in enumerate(self.nav_btns):
            btn.setChecked(i == idx); btn.update()

    def _refresh_badge(self, profile):
        color = PROFILE_COLORS.get(profile, C_ACCENT)
        label = PROFILE_LABELS.get(profile, profile)
        self.badge.setText(label)
        self.badge.setStyleSheet(
            f"color:{color};font-size:10px;font-weight:600;letter-spacing:1px;"
            f"padding:4px 12px;border:1px solid {color};border-radius:10px;"
        )

    def _on_data(self, d):
        """Main-thread signal handler — safe to update UI. Called every 1s."""
        # Always update home (visible or not — keeps badges/OC bar in sync)
        self.home_page.refresh(d)
        self._refresh_badge(d["profile"])
        ac = d["ac"]
        self.ac_ind.setText("⚡ AC" if ac else "🔋 Battery")
        self.ac_ind.setStyleSheet(
            f"color:{C_GREEN if ac else C_ORANGE};font-size:12px;margin-left:12px;"
        )
        # Feed currently visible page if it can accept sampler data
        idx = self.stack.currentIndex()
        if idx == 2:    # PerformancePage
            self.pages[2].refresh(d)
        elif idx == 3:  # DisplayPage — VRR status
            self.pages[3].refresh(d)
        elif idx == 7:  # FanPage — live RPM
            self.pages[7].refresh(d)
        elif idx == 8:  # ActionsPage — power source status
            self.pages[8].refresh(d)

    def _tick(self):
        """Light 2s timer for pages that need periodic refresh but not sampler data."""
        idx = self.stack.currentIndex()
        # Battery page — detailed stats not in sampler
        if idx == 1:
            self.pages[1].refresh()
        # Actions page — AC poll
        elif idx == 8:
            self.pages[8].refresh()

    def closeEvent(self, e):
        global _sampler
        if _sampler:
            _sampler.stop()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Legion Toolkit")
    app.setQuitOnLastWindowClosed(True)

    # Load saved language
    load_language()

    # Load or run hardware detection
    global HW
    if FIRST_RUN_FLAG.exists():
        # Returning user — load saved hardware profile silently
        HW = load_hardware()
        if not HW:
            HW = detect_hardware()
            save_hardware(HW)
    else:
        # First run — show wizard
        wizard = FirstRunWizard()
        wizard.exec()
        HW = load_hardware()
        if not HW:
            HW = detect_hardware()
            save_hardware(HW)
        FIRST_RUN_FLAG.parent.mkdir(parents=True, exist_ok=True)
        FIRST_RUN_FLAG.touch()

    win = LegionDashboard()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
