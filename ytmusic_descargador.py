"""
YT Music Descargador
Busca canciones, álbumes y listas de YouTube Music y descárgalas con yt-dlp
sin tener que recordar comandos.

Versión .exe (GitHub Actions): no necesita instalar nada. Lleva al lado, en la
carpeta «bin», yt-dlp.exe (se actualiza solo al abrir la app), ffmpeg y deno.

Para ejecutarla como script:
    py -m pip install -U ytmusicapi pillow "yt-dlp[default]"
    + ffmpeg y deno en el PATH
"""

import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

try:
    from ytmusicapi import YTMusic
except ImportError:
    YTMusic = None

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None

try:
    import mutagen  # para poner la carátula cuadrada de YouTube Music
except ImportError:
    mutagen = None

APP_NAME = "YT Music Descargador"
APP_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
BIN_DIR = APP_DIR / "bin"
ES_WINDOWS = sys.platform.startswith("win")
SIN_VENTANA = 0x08000000 if ES_WINDOWS else 0  # CREATE_NO_WINDOW: que no salgan consolas negras
EXE = ".exe" if ES_WINDOWS else ""
CONFIG_PATH = Path.home() / ".ytmusic_descargador.json"
MINIATURA = 48  # px de la portada en la lista

# ------------------------------------------------------- paleta (grises oscuros)
C = {
    "fondo": "#121212",
    "panel": "#1a1a1a",
    "campo": "#232323",
    "borde": "#2e2e2e",
    "hover": "#333333",
    "seleccion": "#3d3d3d",
    "texto": "#e6e6e6",
    "tenue": "#8f8f8f",
    "boton": "#e6e6e6",      # botón principal: gris muy claro con texto oscuro
    "boton_txt": "#121212",
}

# Formatos que acepta FFmpegExtractAudio (= --audio-format de yt-dlp)
FORMATOS = {
    "Original (sin recodificar)": "best",
    "MP3": "mp3",
    "M4A (AAC)": "m4a",
    "AAC (ADTS, .m4a)": "aac",
    "OPUS": "opus",
    "OGG Vorbis": "vorbis",
    "FLAC (sin pérdida)": "flac",
    "ALAC (sin pérdida, .m4a)": "alac",
    "WAV (sin comprimir)": "wav",
}
CON_PORTADA = {"best", "mp3", "m4a", "opus", "vorbis", "flac", "alac"}
SIN_CALIDAD = {"best", "flac", "alac", "wav"}

CALIDADES = {
    "Máxima (VBR 0)": "0",
    "320 kbps": "320K",
    "256 kbps": "256K",
    "192 kbps": "192K",
    "128 kbps": "128K",
    "Normal (VBR 5)": "5",
}

TIPOS_BUSQUEDA = {
    "Canciones": "songs",
    "Álbumes": "albums",
    "Playlists": "playlists",
    "Playlists de un canal": "canal",
    "Vídeos": "videos",
}

URL_RE = re.compile(r"^https?://", re.I)


# ----------------------------------------------------------------- utilidades
def limpiar_nombre(texto: str) -> str:
    """Nombre de carpeta válido en Windows/Mac/Linux y seguro para outtmpl."""
    texto = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", texto or "").strip(" .")
    return (texto or "Descargas").replace("%", "%%")


def nombres_artistas(item: dict) -> str:
    artistas = item.get("artists") or []
    if isinstance(artistas, list):
        return ", ".join(a.get("name", "") for a in artistas if a.get("name"))
    return str(artistas)


def url_miniatura(thumbs) -> str | None:
    """La miniatura más pequeña que siga viéndose nítida."""
    if not thumbs:
        return None
    for t in thumbs:
        if (t.get("width") or 0) >= 2 * MINIATURA:
            return t.get("url")
    return thumbs[-1].get("url")


def portada_grande(thumbs) -> str | None:
    """URL de la carátula cuadrada de YT Music en alta resolución (o None si solo hay
    miniatura de vídeo, que es rectangular)."""
    if not thumbs:
        return None
    url = thumbs[-1].get("url") or ""
    if "googleusercontent.com" not in url and "ggpht.com" not in url:
        return None
    base = re.sub(r"=[^/=]*$", "", url) if re.search(r"=[wsh]\d+[^/=]*$", url) else url
    return base + "=w1200-h1200-l90-rj"


def id_de_video(url: str) -> str | None:
    m = re.search(r"[?&]v=([\w-]{11})", url) or re.search(r"youtu\.be/([\w-]{11})", url)
    return m.group(1) if m else None


def id_de_lista(url: str) -> str | None:
    m = re.search(r"[?&]list=([\w-]+)", url)
    return m.group(1) if m else None


def descargar_imagen_jpeg(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        datos = r.read()
    if Image is None:
        return datos
    img = Image.open(io.BytesIO(datos)).convert("RGB")
    salida = io.BytesIO()
    img.save(salida, "JPEG", quality=92)
    return salida.getvalue()


def incrustar_portada(ruta: str, jpeg: bytes) -> bool:
    """Sustituye la carátula del archivo por la imagen dada. Devuelve True si lo consigue."""
    if mutagen is None:
        return False
    import base64
    ext = Path(ruta).suffix.lower()
    if ext == ".mp3":
        from mutagen.id3 import APIC, ID3, ID3NoHeaderError
        try:
            tags = ID3(ruta)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpeg))
        tags.save(ruta, v2_version=3)
    elif ext in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4, MP4Cover
        audio = MP4(ruta)
        audio["covr"] = [MP4Cover(jpeg, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()
    elif ext in (".flac", ".opus", ".ogg"):
        from mutagen.flac import FLAC, Picture
        pic = Picture()
        pic.type, pic.mime, pic.desc, pic.data = 3, "image/jpeg", "Cover", jpeg
        if Image is not None:
            pic.width, pic.height = Image.open(io.BytesIO(jpeg)).size
            pic.depth = 24
        if ext == ".flac":
            audio = FLAC(ruta)
            audio.clear_pictures()
            audio.add_picture(pic)
        else:
            from mutagen.oggopus import OggOpus
            from mutagen.oggvorbis import OggVorbis
            audio = OggOpus(ruta) if ext == ".opus" else OggVorbis(ruta)
            audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        audio.save()
    else:
        return False
    return True


def con_id(artistas) -> list:
    """Solo los artistas que tienen página propia en YT Music (enlazables)."""
    if not isinstance(artistas, list):
        return []
    return [a for a in artistas if a.get("id") and a.get("name")]


def fila_cancion(t: dict, thumb=None, album=None, info=None, portada=None):
    """Convierte una pista (de búsqueda, álbum o playlist) en una fila de la tabla."""
    if not t.get("videoId") or t.get("isAvailable") is False:
        return None
    alb = t.get("album") or album
    alb = alb if isinstance(alb, dict) else None
    titulo = t.get("title") or "?"
    return {
        "fila": (titulo, nombres_artistas(t), info or (alb or {}).get("name") or "",
                 t.get("duration") or ""),
        "thumb": url_miniatura(t.get("thumbnails")) or thumb,
        "datos": {"tipo": "cancion", "coleccion": False, "titulo": titulo,
                  "url": f"https://music.youtube.com/watch?v={t['videoId']}",
                  "artistas": con_id(t.get("artists")),
                  "album": alb if alb and alb.get("id") else None,
                  "portada": portada_grande(t.get("thumbnails")) or portada},
    }


def abrir_carpeta(ruta: str):
    Path(ruta).mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(ruta)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", ruta])
    else:
        subprocess.Popen(["xdg-open", ruta])


def barra_titulo_oscura(ventana):
    """En Windows 10/11 pone la barra de título en modo oscuro."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        ventana.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(ventana.winfo_id())
        valor = ctypes.c_int(1)
        for atributo in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (nuevo / antiguo)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, atributo, ctypes.byref(valor), ctypes.sizeof(valor)) == 0:
                break
    except Exception:
        pass


def cargar_config() -> dict:
    por_defecto = {
        "carpeta": str(Path.home() / "Music" / "YT Music"),
        "formato": "MP3",
        "calidad": "Máxima (VBR 0)",
        "metadatos": True,
        "portada_cuadrada": True,
        "tipo": "Canciones",
    }
    try:
        por_defecto.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    if por_defecto["tipo"] not in TIPOS_BUSQUEDA:
        por_defecto["tipo"] = "Canciones"
    return por_defecto


def binario(nombre: str) -> str | None:
    """Busca un programa primero en la carpeta bin de la app y luego en el PATH."""
    local = BIN_DIR / (nombre + EXE)
    return str(local) if local.exists() else shutil.which(nombre)


def comando_ytdlp() -> list | None:
    exe = binario("yt-dlp")
    if exe:
        return [exe]
    try:  # modo script: yt-dlp instalado con pip
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        return None


def ejecutar(args, **kw):
    """Lanza un proceso sin abrir ventana de consola y leyendo su salida en UTF-8."""
    entorno = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                            errors="replace", env=entorno, creationflags=SIN_VENTANA, **kw)


# ------------------------------------------------------------------- estilos
def aplicar_tema(root: tk.Tk):
    familia = "Segoe UI" if sys.platform.startswith("win") else (
        "SF Pro Text" if sys.platform == "darwin" else "DejaVu Sans")
    for nombre in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        try:
            tkfont.nametofont(nombre).configure(family=familia, size=10)
        except tk.TclError:
            pass

    root.configure(bg=C["fondo"])
    # Lista desplegable de los Combobox (no es ttk, se configura aparte)
    root.option_add("*TCombobox*Listbox.background", C["campo"])
    root.option_add("*TCombobox*Listbox.foreground", C["texto"])
    root.option_add("*TCombobox*Listbox.selectBackground", C["seleccion"])
    root.option_add("*TCombobox*Listbox.selectForeground", C["texto"])
    root.option_add("*TCombobox*Listbox.borderWidth", 0)

    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=C["fondo"], foreground=C["texto"], fieldbackground=C["campo"],
                bordercolor=C["borde"], lightcolor=C["borde"], darkcolor=C["borde"],
                troughcolor=C["panel"], focuscolor=C["seleccion"], insertcolor=C["texto"],
                selectbackground=C["seleccion"], selectforeground=C["texto"])
    s.configure("TFrame", background=C["fondo"])
    s.configure("Panel.TFrame", background=C["panel"])
    s.configure("TLabel", background=C["fondo"], foreground=C["texto"])
    s.configure("Panel.TLabel", background=C["panel"])
    s.configure("Tenue.TLabel", foreground=C["tenue"])
    s.configure("PanelTenue.TLabel", background=C["panel"], foreground=C["tenue"])
    s.configure("Titulo.TLabel", font=(familia, 15, "bold"))

    s.configure("TButton", background=C["campo"], foreground=C["texto"], padding=(12, 6),
                borderwidth=1, relief="flat")
    s.map("TButton", background=[("pressed", C["seleccion"]), ("active", C["hover"])],
          bordercolor=[("focus", C["tenue"])])
    s.configure("Principal.TButton", background=C["boton"], foreground=C["boton_txt"],
                font=(familia, 10, "bold"), padding=(16, 7))
    s.map("Principal.TButton", background=[("pressed", "#b5b5b5"), ("active", "#ffffff")])

    s.configure("TEntry", padding=7, fieldbackground=C["campo"], foreground=C["texto"])
    s.map("TEntry", bordercolor=[("focus", C["tenue"])], lightcolor=[("focus", C["tenue"])])
    s.configure("TCombobox", padding=5, arrowcolor=C["tenue"], background=C["campo"],
                fieldbackground=C["campo"], foreground=C["texto"])
    s.map("TCombobox",
          fieldbackground=[("readonly", C["campo"]), ("disabled", C["panel"])],
          foreground=[("disabled", C["tenue"]), ("readonly", C["texto"])],
          background=[("active", C["hover"])], arrowcolor=[("disabled", C["borde"])],
          selectbackground=[("readonly", C["campo"])], selectforeground=[("readonly", C["texto"])])

    s.configure("TCheckbutton", background=C["panel"], foreground=C["texto"],
                indicatorbackground=C["campo"], indicatorforeground=C["texto"])
    s.map("TCheckbutton", background=[("active", C["panel"])],
          indicatorbackground=[("selected", C["seleccion"]), ("active", C["hover"])])

    s.configure("Treeview", background=C["panel"], fieldbackground=C["panel"],
                foreground=C["texto"], rowheight=MINIATURA + 12, borderwidth=0)
    s.map("Treeview", background=[("selected", C["seleccion"])],
          foreground=[("selected", "#ffffff")])
    s.configure("Treeview.Heading", background=C["fondo"], foreground=C["tenue"],
                borderwidth=0, relief="flat", padding=(6, 6))
    s.map("Treeview.Heading", background=[("active", C["fondo"])])
    s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])  # sin marco

    s.configure("Vertical.TScrollbar", background=C["campo"], troughcolor=C["panel"],
                arrowcolor=C["tenue"], borderwidth=0)
    s.map("Vertical.TScrollbar", background=[("active", C["hover"])])
    s.configure("Horizontal.TProgressbar", background=C["texto"], troughcolor=C["campo"],
                borderwidth=0, thickness=6)
    return familia


# ---------------------------------------------------------------- aplicación
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1040x720")
        self.minsize(860, 560)
        self.familia = aplicar_tema(self)
        barra_titulo_oscura(self)
        icono = Path(getattr(sys, "_MEIPASS", APP_DIR)) / "icono.ico"
        if ES_WINDOWS and icono.exists():
            try:
                self.iconbitmap(default=str(icono))
            except tk.TclError:
                pass

        self.cfg = cargar_config()
        self.ytm = None
        self.resultados = {}          # iid -> dict con info del resultado
        self.imagenes = {}            # iid -> PhotoImage (hay que guardar la referencia)
        self.generacion = 0           # descarta miniaturas de búsquedas anteriores
        self.cache_img = {}           # url -> imagen ya descargada
        self.historial = []           # vistas anteriores para «Volver»
        self.vista = None             # (título, filas) de lo que se ve ahora
        self.navegando = False
        self.pool_miniaturas = ThreadPoolExecutor(max_workers=6)
        self.cola_ui = queue.Queue()
        self.cola_descargas = queue.Queue()

        self._construir_ui()
        self._comprobar_dependencias()
        threading.Thread(target=self._trabajador, daemon=True).start()
        self.after(100, self._procesar_cola_ui)
        self.protocol("WM_DELETE_WINDOW", self._cerrar)

    def _get_ytm(self):
        if self.ytm is None:
            self.ytm = YTMusic()
        return self.ytm

    # ---------------------------------------------------------------- interfaz
    def _construir_ui(self):
        # Cabecera + búsqueda
        cab = ttk.Frame(self, padding=(16, 14, 16, 8))
        cab.pack(fill="x")
        ttk.Label(cab, text="YT Music Descargador", style="Titulo.TLabel").pack(anchor="w")
        ttk.Label(cab, text="Busca, selecciona y descarga. Doble clic en un resultado para "
                            "descargarlo directamente.", style="Tenue.TLabel").pack(anchor="w")

        fila = ttk.Frame(cab)
        fila.pack(fill="x", pady=(10, 0))
        self.var_busqueda = tk.StringVar()
        entrada = ttk.Entry(fila, textvariable=self.var_busqueda, font=(self.familia, 11))
        entrada.pack(side="left", fill="x", expand=True)
        entrada.bind("<Return>", lambda e: self.buscar())
        entrada.focus_set()
        self.var_tipo = tk.StringVar(value=self.cfg["tipo"])
        cb_tipo = ttk.Combobox(fila, textvariable=self.var_tipo, values=list(TIPOS_BUSQUEDA),
                               state="readonly", width=20)
        cb_tipo.pack(side="left", padx=8)
        cb_tipo.bind("<<ComboboxSelected>>", lambda e: self._actualizar_pista())
        ttk.Button(fila, text="Buscar", style="Principal.TButton",
                   command=self.buscar).pack(side="left")
        self.var_pista = tk.StringVar()
        ttk.Label(cab, textvariable=self.var_pista, style="Tenue.TLabel").pack(anchor="w", pady=(4, 0))
        self._actualizar_pista()

        # Barra de navegación (qué se está viendo + volver)
        f_nav = ttk.Frame(self, padding=(16, 2))
        f_nav.pack(fill="x")
        self.btn_volver = ttk.Button(f_nav, text="←  Volver", command=self.volver)
        self.var_vista = tk.StringVar(value="")
        ttk.Label(f_nav, textvariable=self.var_vista, font=(self.familia, 11, "bold"))\
            .pack(side="left", pady=4)
        ttk.Label(f_nav, text="Ctrl+A selecciona todo", style="Tenue.TLabel").pack(side="right")

        # Resultados
        f_res = ttk.Frame(self, style="Panel.TFrame", padding=1)
        f_res.pack(fill="both", expand=True, padx=16, pady=4)
        cols = ("titulo", "artista", "info", "duracion")
        self.tabla = ttk.Treeview(f_res, columns=cols, show="tree headings", selectmode="extended",
                                  height=4)
        self.tabla.heading("#0", text="")
        self.tabla.column("#0", width=MINIATURA + 24, stretch=False, anchor="center")
        for col, txt, ancho in [("titulo", "TÍTULO", 330), ("artista", "ARTISTA / CANAL", 220),
                                ("info", "ÁLBUM / INFO", 240), ("duracion", "DURACIÓN", 80)]:
            self.tabla.heading(col, text=txt, anchor="w")
            self.tabla.column(col, width=ancho, anchor="w")
        barra = ttk.Scrollbar(f_res, orient="vertical", command=self.tabla.yview)
        self.tabla.configure(yscrollcommand=barra.set)
        self.tabla.pack(side="left", fill="both", expand=True)
        barra.pack(side="right", fill="y")
        self.tabla.bind("<Double-1>", self._doble_clic)
        self.tabla.bind("<ButtonRelease-1>", self._clic_tabla)
        self.tabla.bind("<Motion>", self._mover_raton)
        self.tabla.bind("<Control-a>", lambda e: self.tabla.selection_set(self.tabla.get_children()))
        self.tabla.bind("<BackSpace>", lambda e: self.volver())
        self.menu = tk.Menu(self, tearoff=0, bg=C["campo"], fg=C["texto"], bd=0,
                            activebackground=C["seleccion"], activeforeground="#ffffff")

        # Opciones
        f_opt = ttk.Frame(self, style="Panel.TFrame", padding=(12, 10))
        f_opt.pack(fill="x", padx=16, pady=(8, 4))
        pad = {"padx": 6, "pady": 4}
        ttk.Label(f_opt, text="Formato", style="PanelTenue.TLabel").grid(row=0, column=0, sticky="w", **pad)
        self.var_formato = tk.StringVar(value=self.cfg["formato"])
        cb_fmt = ttk.Combobox(f_opt, textvariable=self.var_formato, values=list(FORMATOS),
                              state="readonly", width=26)
        cb_fmt.grid(row=0, column=1, sticky="w", **pad)
        cb_fmt.bind("<<ComboboxSelected>>", lambda e: self._actualizar_calidad())

        ttk.Label(f_opt, text="Calidad", style="PanelTenue.TLabel").grid(row=0, column=2, sticky="w", **pad)
        self.var_calidad = tk.StringVar(value=self.cfg["calidad"])
        self.cb_calidad = ttk.Combobox(f_opt, textvariable=self.var_calidad,
                                       values=list(CALIDADES), state="readonly", width=16)
        self.cb_calidad.grid(row=0, column=3, sticky="w", **pad)

        self.var_meta = tk.BooleanVar(value=self.cfg["metadatos"])
        ttk.Checkbutton(f_opt, text="Metadatos y portada", variable=self.var_meta)\
            .grid(row=0, column=4, sticky="w", **pad)
        self.var_cuadrada = tk.BooleanVar(value=self.cfg["portada_cuadrada"])
        ttk.Checkbutton(f_opt, text="Portada cuadrada", variable=self.var_cuadrada)\
            .grid(row=0, column=5, sticky="w", **pad)

        ttk.Label(f_opt, text="Carpeta", style="PanelTenue.TLabel").grid(row=1, column=0, sticky="w", **pad)
        self.var_carpeta = tk.StringVar(value=self.cfg["carpeta"])
        ttk.Entry(f_opt, textvariable=self.var_carpeta).grid(row=1, column=1, columnspan=3,
                                                              sticky="ew", **pad)
        ttk.Button(f_opt, text="Cambiar…", command=self.elegir_carpeta).grid(row=1, column=4, sticky="w", **pad)
        ttk.Button(f_opt, text="Abrir carpeta",
                   command=lambda: abrir_carpeta(self.var_carpeta.get())).grid(row=1, column=5, sticky="w", **pad)
        f_opt.columnconfigure(3, weight=1)
        self._actualizar_calidad()

        # Acciones
        f_acc = ttk.Frame(self, padding=(16, 6))
        f_acc.pack(fill="x")
        ttk.Button(f_acc, text="⬇  Descargar seleccionados", style="Principal.TButton",
                   command=self.descargar_seleccion).pack(side="left")
        ttk.Button(f_acc, text="Elegir stream exacto…",
                   command=self.ver_formatos).pack(side="left", padx=8)
        ttk.Button(f_acc, text="Actualizar yt-dlp",
                   command=self.actualizar_ytdlp).pack(side="right")

        # Progreso y registro
        f_prog = ttk.Frame(self, padding=(16, 4))
        f_prog.pack(fill="x")
        self.var_estado = tk.StringVar(value="Listo.")
        ttk.Label(f_prog, textvariable=self.var_estado, style="Tenue.TLabel").pack(side="left")
        self.progreso = ttk.Progressbar(f_prog, mode="determinate", maximum=100)
        self.progreso.pack(side="right", fill="x", expand=True, padx=(16, 0))

        self.registro = tk.Text(self, height=7, state="disabled", wrap="word",
                                bg=C["panel"], fg=C["tenue"], insertbackground=C["texto"],
                                selectbackground=C["seleccion"], relief="flat",
                                highlightthickness=0, padx=10, pady=8,
                                font=("Consolas" if sys.platform.startswith("win") else "monospace", 9))
        self.registro.pack(fill="both", padx=16, pady=(4, 14))
        fuente_log = tkfont.Font(font=self.registro.cget("font"))
        negrita = fuente_log.copy()
        negrita.configure(weight="bold")
        self.registro.tag_configure("ok", foreground="#4ade80", font=negrita)      # verde
        self.registro.tag_configure("error", foreground="#f87171", font=negrita)   # rojo
        self.registro.tag_configure("aviso", foreground="#fbbf24")                # ámbar

    def _actualizar_pista(self):
        pistas = {
            "canal": "Escribe el nombre del canal (o usuario) y verás todas sus playlists públicas.",
            "playlists": "Busca por nombre de la playlist. Si es tuya, prueba «Playlists de un canal».",
        }
        self.var_pista.set(pistas.get(TIPOS_BUSQUEDA[self.var_tipo.get()],
                                      "Clic en un artista o álbum de la lista para ver sus canciones. "
                                      "También puedes pegar un enlace de YouTube / YT Music."))

    def _actualizar_calidad(self):
        codec = FORMATOS[self.var_formato.get()]
        self.cb_calidad.configure(state="disabled" if codec in SIN_CALIDAD else "readonly")

    def log(self, texto):
        """Escribe en el registro. El «✔ Terminado» va en verde y los errores en rojo."""
        if texto.startswith("✔"):
            etiqueta = "ok"
        elif texto.startswith("✖") or texto.startswith("⚠ Terminado con errores"):
            etiqueta = "error"
        elif texto.startswith("⚠"):
            etiqueta = "aviso"
        else:
            etiqueta = None
        self.registro.configure(state="normal")
        if etiqueta:
            # se colorea el símbolo y la palabra clave (hasta los dos puntos)
            corte = texto.find(":") + 1 or len(texto)
            self.registro.insert("end", texto[:corte], etiqueta)
            self.registro.insert("end", texto[corte:] + "\n")
        else:
            self.registro.insert("end", texto + "\n")
        self.registro.see("end")
        self.registro.configure(state="disabled")

    def enviar(self, tipo, dato=None):
        self.cola_ui.put((tipo, dato))

    def _procesar_cola_ui(self):
        try:
            while True:
                tipo, dato = self.cola_ui.get_nowait()
                if tipo == "log":
                    self.log(dato)
                elif tipo == "estado":
                    self.var_estado.set(dato)
                elif tipo == "progreso":
                    self.progreso["value"] = dato
                elif tipo == "resultados":
                    self._mostrar_resultados(*dato)
                elif tipo == "miniatura":
                    self._poner_miniatura(*dato)
                elif tipo == "formatos":
                    self._dialogo_formatos(*dato)
                elif tipo == "error":
                    messagebox.showerror(APP_NAME, dato)
        except queue.Empty:
            pass
        self.after(80, self._procesar_cola_ui)

    def _comprobar_dependencias(self):
        self.ytdlp = comando_ytdlp()
        if self.ytdlp is None:
            self.log("✖ Falta yt-dlp: pon yt-dlp.exe en la carpeta «bin» o instálalo con pip.")
        if YTMusic is None:
            self.log("✖ Falta ytmusicapi (para buscar):  py -m pip install ytmusicapi")
        if Image is None:
            self.log("⚠ Falta Pillow, no se verán las portadas:  py -m pip install pillow")
        if not binario("ffmpeg"):
            self.log("⚠ No encuentro ffmpeg: sin él no se puede convertir ni poner portadas.")
        if not any(binario(x) for x in ("deno", "node", "bun", "qjs")):
            self.log("⚠ No encuentro deno: yt-dlp lo necesita para YouTube.")
        if self.ytdlp:
            # Al abrir: comprobar la versión y, si es el .exe, actualizarlo solo
            threading.Thread(target=self._actualizar_hilo, args=(True,), daemon=True).start()

    # ---------------------------------------------------------------- búsqueda
    def buscar(self):
        texto = self.var_busqueda.get().strip()
        if not texto:
            return
        if URL_RE.match(texto):
            es_lista = "list=" in texto or "/playlist" in texto or "/browse/" in texto
            iid = self.tabla.insert("", 0, values=(texto, "", "Enlace pegado", ""))
            self.resultados[iid] = {"tipo": "url", "url": texto, "coleccion": es_lista,
                                    "titulo": "Enlace"}
            self.tabla.selection_set(iid)
            return
        if YTMusic is None:
            messagebox.showerror(APP_NAME, "Para buscar necesitas instalar ytmusicapi:\n\n"
                                           "py -m pip install ytmusicapi")
            return
        filtro = TIPOS_BUSQUEDA[self.var_tipo.get()]
        self.navegando = False
        self.generacion += 1
        self.var_estado.set(f"Buscando «{texto}»…")
        threading.Thread(target=self._buscar_hilo, args=(texto, filtro, self.generacion),
                         daemon=True).start()

    def _buscar_hilo(self, texto, filtro, gen):
        try:
            ytm = self._get_ytm()
            if filtro == "canal":
                filas = self._playlists_de_canal(ytm, texto)
            else:
                filas = [f for f in (self._normalizar(r, filtro)
                                     for r in ytm.search(texto, filter=filtro, limit=30)) if f]
            self.enviar("resultados", (gen, filas, f"Resultados de «{texto}»", False))
            self.enviar("estado", f"{len(filas)} resultados." if filas else "Sin resultados.")
        except Exception as e:
            self.enviar("estado", "Error en la búsqueda.")
            self.enviar("error", f"No se pudo buscar:\n{e}")

    def _playlists_de_canal(self, ytm, texto):
        """Busca canales/usuarios con ese nombre y devuelve sus playlists públicas."""
        perfiles = [p for p in ytm.search(texto, filter="profiles", limit=5) if p.get("browseId")]
        filas = []
        for perfil in perfiles[:3]:
            canal_id = perfil["browseId"]
            nombre_canal = perfil.get("title") or perfil.get("name") or canal_id
            self.enviar("estado", f"Leyendo playlists de «{nombre_canal}»…")
            try:
                usuario = ytm.get_user(canal_id)
            except Exception:
                continue
            bloque = usuario.get("playlists") or {}
            listas = bloque.get("results") or []
            if bloque.get("params"):  # hay más de las que se ven en la portada del canal
                try:
                    listas = ytm.get_user_playlists(canal_id, bloque["params"]) or listas
                except Exception:
                    pass
            for pl in listas:
                pid = pl.get("playlistId") or ""
                if not pid:
                    continue
                filas.append({
                    "fila": (pl.get("title") or "?", usuario.get("name") or nombre_canal,
                             "Playlist del canal", ""),
                    "datos": {"tipo": "playlist", "coleccion": True, "titulo": pl.get("title") or pid,
                              "url": f"https://music.youtube.com/playlist?list={pid}"},
                    "thumb": url_miniatura(pl.get("thumbnails")),
                })
        if not perfiles:
            self.enviar("log", f"No he encontrado ningún canal llamado «{texto}».")
        return filas

    @staticmethod
    def _normalizar(r, filtro):
        titulo = r.get("title", "?")
        artista = nombres_artistas(r)
        thumb = url_miniatura(r.get("thumbnails"))
        if filtro == "songs":
            return fila_cancion(r)
        if filtro == "videos":
            if not r.get("videoId"):
                return None
            return {"fila": (titulo, artista, "Vídeo", r.get("duration") or ""), "thumb": thumb,
                    "datos": {"tipo": "cancion", "coleccion": False, "titulo": titulo,
                              "url": f"https://music.youtube.com/watch?v={r['videoId']}"}}
        if filtro == "albums":
            if not r.get("browseId"):
                return None
            info = f"{r.get('type') or 'Álbum'} {r.get('year') or ''}".strip()
            return {"fila": (titulo, artista, info, ""), "thumb": thumb,
                    "datos": {"tipo": "album", "coleccion": True, "titulo": titulo,
                              "browseId": r["browseId"], "artista": artista,
                              "artistas": con_id(r.get("artists")),
                              "album": {"name": titulo, "id": r["browseId"]},
                              "portada": portada_grande(r.get("thumbnails"))}}
        # playlists
        bid = r.get("browseId") or ""
        if not bid:
            return None
        lista = bid[2:] if bid.startswith("VL") else bid
        autor = r.get("author") or artista
        if isinstance(autor, list):
            autor = ", ".join(a.get("name", "") for a in autor)
        cuenta = r.get("itemCount")
        info = f"Playlist · {cuenta} canciones" if cuenta else "Playlist"
        return {"fila": (titulo, autor or "", info, ""), "thumb": thumb,
                "datos": {"tipo": "playlist", "coleccion": True, "titulo": titulo,
                          "url": f"https://music.youtube.com/playlist?list={lista}"}}

    def _mostrar_resultados(self, gen, filas, titulo="", apilar=False):
        if gen != self.generacion:
            return
        self.navegando = False
        if apilar and self.vista:
            self.historial.append(self.vista)
        elif not apilar:
            self.historial.clear()
        self.vista = (titulo, filas)
        self._pintar_vista()

    def _pintar_vista(self):
        titulo, filas = self.vista
        self.var_vista.set(titulo)
        if self.historial:
            self.btn_volver.pack(side="left", padx=(0, 10), before=self.btn_volver.master.winfo_children()[1])
        else:
            self.btn_volver.pack_forget()
        self.tabla.delete(*self.tabla.get_children())
        self.resultados.clear()
        self.imagenes.clear()
        gen = self.generacion
        for f in filas:
            iid = self.tabla.insert("", "end", values=f["fila"])
            self.resultados[iid] = f["datos"]
            url = f.get("thumb")
            if url and Image is not None:
                if url in self.cache_img:
                    self._poner_miniatura(gen, iid, self.cache_img[url])
                else:
                    self.pool_miniaturas.submit(self._descargar_miniatura, gen, iid, url)
        self.tabla.yview_moveto(0)

    def volver(self):
        if not self.historial or self.navegando:
            return
        self.generacion += 1
        self.vista = self.historial.pop()
        self._pintar_vista()
        self.var_estado.set("Listo.")

    def _descargar_miniatura(self, gen, iid, url):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                img = Image.open(io.BytesIO(r.read())).convert("RGB")
            lado = min(img.size)  # recorte cuadrado centrado (las de vídeo son 16:9)
            x, y = (img.width - lado) // 2, (img.height - lado) // 2
            img = img.crop((x, y, x + lado, y + lado)).resize((MINIATURA, MINIATURA), Image.LANCZOS)
            self.cache_img[url] = img
            self.enviar("miniatura", (gen, iid, img))
        except Exception:
            pass

    # ------------------------------------------- clic en artista / álbum
    def _enlace_en(self, x, y):
        """Devuelve ('artista', [..]) o ('album', {...}) si el ratón está sobre un enlace."""
        if self.tabla.identify_region(x, y) != "cell":
            return None
        iid = self.tabla.identify_row(y)
        datos = self.resultados.get(iid) or {}
        col = self.tabla.identify_column(x)
        if col == "#2" and datos.get("artistas"):
            return ("artista", datos["artistas"])
        if col == "#3" and datos.get("album"):
            return ("album", datos["album"])
        return None

    def _mover_raton(self, e):
        self.tabla.configure(cursor="hand2" if self._enlace_en(e.x, e.y) else "")

    def _clic_tabla(self, e):
        if e.state & 0x0005:  # con Ctrl o Mayús se está seleccionando, no navegando
            return
        enlace = self._enlace_en(e.x, e.y)
        if not enlace or self.navegando:
            return
        tipo, valor = enlace
        if tipo == "album":
            self.abrir_album(valor)
        elif len(valor) == 1:
            self.abrir_artista(valor[0])
        else:  # varios artistas: que elija
            self.menu.delete(0, "end")
            for a in valor:
                self.menu.add_command(label=f"Canciones de {a['name']}",
                                      command=lambda a=a: self.abrir_artista(a))
            self.menu.tk_popup(e.x_root, e.y_root)

    def _doble_clic(self, e):
        if self._enlace_en(e.x, e.y):  # el doble clic sobre un enlace ya navegó
            return "break"
        self.descargar_seleccion()

    def _navegar(self, texto_estado, funcion, *args):
        if YTMusic is None:
            return
        self.navegando = True
        self.generacion += 1
        self.var_estado.set(texto_estado)
        gen = self.generacion

        def hilo():
            try:
                titulo, filas = funcion(self._get_ytm(), *args)
                self.enviar("resultados", (gen, filas, titulo, True))
                self.enviar("estado", f"{len(filas)} canciones." if filas else "Sin canciones.")
            except Exception as ex:
                self.navegando = False
                self.enviar("estado", "No se pudo cargar.")
                self.enviar("error", f"No se pudo cargar:\n{ex}")

        threading.Thread(target=hilo, daemon=True).start()

    def abrir_artista(self, artista):
        self._navegar(f"Cargando canciones de {artista['name']}…", self._canciones_artista, artista)

    def abrir_album(self, album):
        self._navegar(f"Cargando «{album['name']}»…", self._canciones_album, album)

    @staticmethod
    def _canciones_artista(ytm, artista):
        info = ytm.get_artist(artista["id"])
        nombre = info.get("name") or artista["name"]
        bloque = info.get("songs") or {}
        pistas = bloque.get("results") or []
        if bloque.get("browseId"):  # lista completa de canciones del artista
            try:
                pistas = ytm.get_playlist(bloque["browseId"], limit=300).get("tracks") or pistas
            except Exception:
                pass
        filas = [f for f in (fila_cancion(t) for t in pistas) if f]
        return f"Artista: {nombre} · todas sus canciones", filas

    @staticmethod
    def _canciones_album(ytm, album):
        info = ytm.get_album(album["id"])
        nombre = info.get("title") or album["name"]
        portada = url_miniatura(info.get("thumbnails"))
        alb = {"name": nombre, "id": album["id"]}
        filas = []
        for t in info.get("tracks") or []:
            n = t.get("trackNumber")
            f = fila_cancion(t, thumb=portada, album=alb,
                             info=f"{n}. {nombre}" if n else nombre,
                             portada=portada_grande(info.get("thumbnails")))
            if f:
                f["datos"]["album"] = alb
                filas.append(f)
        artistas = nombres_artistas(info)
        return f"Álbum: {nombre}" + (f" · {artistas}" if artistas else ""), filas

    def _poner_miniatura(self, gen, iid, img):
        if gen != self.generacion or not self.tabla.exists(iid):
            return
        foto = ImageTk.PhotoImage(img)  # debe crearse en el hilo de la interfaz
        self.imagenes[iid] = foto
        self.tabla.item(iid, image=foto)

    # --------------------------------------------------------------- descargas
    def elegir_carpeta(self):
        ruta = filedialog.askdirectory(initialdir=self.var_carpeta.get() or str(Path.home()))
        if ruta:
            self.var_carpeta.set(ruta)

    def _opciones_actuales(self):
        return {
            "carpeta": self.var_carpeta.get().strip() or str(Path.home() / "Music"),
            "codec": FORMATOS[self.var_formato.get()],
            "calidad": CALIDADES.get(self.var_calidad.get(), "0"),
            "metadatos": self.var_meta.get(),
            "cuadrada": self.var_cuadrada.get(),
        }

    def descargar_seleccion(self):
        if self.ytdlp is None:
            messagebox.showerror(APP_NAME, "No encuentro yt-dlp. Vuelve a descargar la aplicación.")
            return
        sel = self.tabla.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "Selecciona al menos un resultado (Ctrl/Mayús para varios).")
            return
        opts = self._opciones_actuales()
        for iid in sel:
            self.cola_descargas.put(("descargar", dict(self.resultados[iid]), opts, None))
        self.log(f"➕ {len(sel)} elemento(s) en cola.")

    def ver_formatos(self):
        sel = self.tabla.selection()
        if len(sel) != 1 or self.resultados[sel[0]].get("coleccion"):
            messagebox.showinfo(APP_NAME, "Selecciona UNA canción para ver todos sus streams disponibles.")
            return
        self.var_estado.set("Consultando formatos disponibles…")
        self.cola_descargas.put(("formatos", dict(self.resultados[sel[0]]), None, None))

    def _dialogo_formatos(self, item, formatos):
        win = tk.Toplevel(self)
        win.title(f"Streams de audio · {item['titulo']}")
        win.geometry("760x400")
        win.configure(bg=C["fondo"])
        barra_titulo_oscura(win)
        marco = ttk.Frame(win, padding=14)
        marco.pack(fill="both", expand=True)
        ttk.Label(marco, text=item["titulo"], style="Titulo.TLabel").pack(anchor="w")
        ttk.Label(marco, text="Se descarga el stream elegido y luego se aplica el formato de "
                              "«Opciones» (elige «Original» para guardarlo tal cual).",
                  style="Tenue.TLabel").pack(anchor="w", pady=(0, 8))
        s = ttk.Style(win)
        s.configure("Formatos.Treeview", rowheight=26)
        cols = ("id", "ext", "codec", "kbps", "hz", "tamano", "nota")
        tv = ttk.Treeview(marco, columns=cols, show="headings", selectmode="browse",
                          style="Formatos.Treeview")
        for c, t, w in [("id", "ID", 70), ("ext", "EXT", 60), ("codec", "CÓDEC", 110),
                        ("kbps", "KBPS", 60), ("hz", "HZ", 70), ("tamano", "TAMAÑO", 90),
                        ("nota", "NOTA", 220)]:
            tv.heading(c, text=t, anchor="w")
            tv.column(c, width=w, anchor="w")
        for f in formatos:
            tam = f.get("filesize") or f.get("filesize_approx")
            tv.insert("", "end", iid=f["format_id"], values=(
                f["format_id"], f.get("ext", ""), f.get("acodec", ""),
                round(f["abr"]) if f.get("abr") else "", f.get("asr") or "",
                f"{tam / 1048576:.1f} MB" if tam else "", f.get("format_note") or ""))
        tv.pack(fill="both", expand=True)

        def descargar():
            sel = tv.selection()
            if not sel:
                return
            self.cola_descargas.put(("descargar", item, self._opciones_actuales(), sel[0]))
            self.log(f"➕ {item['titulo']} (stream {sel[0]}) en cola.")
            win.destroy()

        ttk.Button(marco, text="⬇  Descargar este stream", style="Principal.TButton",
                   command=descargar).pack(pady=(10, 0), anchor="e")
        tv.bind("<Double-1>", lambda e: descargar())

    def _argumentos_comunes(self):
        args = []
        ffmpeg = binario("ffmpeg")
        if ffmpeg:
            args += ["--ffmpeg-location", ffmpeg]
        deno = binario("deno")
        if deno:
            args += ["--js-runtimes", f"deno:{deno}"]
        return args

    def _construir_comando(self, item, opts, format_id):
        """Equivale a lo que escribirías a mano en la consola con yt-dlp."""
        codec = opts["codec"]
        if item["coleccion"]:
            if item["tipo"] == "url":
                nombre = "%(playlist_title,album|Lista)s"
            elif item["tipo"] == "album" and item.get("artista"):
                nombre = limpiar_nombre(f"{item['artista']} - {item['titulo']}")
            else:
                nombre = limpiar_nombre(item.get("titulo"))
            plantilla = (nombre + "/%(playlist_index)s - %(artist,uploader|Desconocido)s"
                                  " - %(track,title)s.%(ext)s")
        else:
            plantilla = "%(artist,uploader|Desconocido)s - %(track,title)s.%(ext)s"

        args = self.ytdlp + self._argumentos_comunes() + [
            "-f", format_id or "ba/b",
            "-P", str(Path(opts["carpeta"])),
            "-o", plantilla,
            "-o", "pl_thumbnail:",
            "--windows-filenames",
            "--yes-playlist" if item["coleccion"] else "--no-playlist",
            "--print", "after_move:[FILE] %(id)s %(filepath)s",
            "--no-quiet", "--progress",
            "--newline",
            "--progress-template",
            "download:[PROG] %(progress.downloaded_bytes|0)s %(progress.total_bytes,progress.total_bytes_estimate|0)s "
            "%(info.playlist_index|)s %(info.n_entries|)s %(info.track,info.title|)s",
            "-x", "--audio-format", codec,
        ]
        if codec not in SIN_CALIDAD:
            args += ["--audio-quality", opts["calidad"]]
        if opts["metadatos"]:
            args += ["--embed-metadata"]
            if codec in CON_PORTADA:
                args += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]
                if opts["cuadrada"]:
                    args += ["--ppa", "ThumbnailsConvertor+FFmpeg_o:-c:v mjpeg -vf "
                                      "crop=\"'if(gt(ih,iw),iw,ih)':'if(gt(iw,ih),ih,iw)'\""]
        return args + ["--", item["url"]]

    def _leer_salida(self, linea):
        """Interpreta cada línea que escribe yt-dlp."""
        linea = linea.rstrip()
        if not linea:
            return
        if linea.startswith("[PROG] "):
            partes = linea[7:].split(" ", 4) + [""] * 5
            hecho, total, idx, n, nombre = partes[:5]
            try:
                if float(total) > 0:
                    self.enviar("progreso", float(hecho) * 100 / float(total))
            except ValueError:
                pass
            prefijo = f"[{idx}/{n}] " if idx and n and idx != "NA" and n != "NA" else ""
            self.enviar("estado", f"{prefijo}Descargando: {nombre}")
        elif linea.startswith("ERROR:"):
            self.enviar("log", "✖ " + linea)
        elif linea.startswith("WARNING:"):
            self.enviar("log", "⚠ " + linea[8:].strip())
        elif linea.startswith("[ExtractAudio]"):
            self.enviar("estado", "Convirtiendo audio…")
            self.enviar("log", linea)
        elif not linea.startswith("[debug]"):
            self.enviar("log", linea)

    # -------------------------------------------------------- hilo de trabajo
    def _trabajador(self):
        while True:
            accion, item, opts, format_id = self.cola_descargas.get()
            try:
                if item["tipo"] == "album" and "url" not in item:
                    album = self._get_ytm().get_album(item["browseId"])
                    item["url"] = f"https://music.youtube.com/playlist?list={album['audioPlaylistId']}"
                    item["portada"] = item.get("portada") or portada_grande(album.get("thumbnails"))

                if accion == "formatos":
                    p = ejecutar(self.ytdlp + self._argumentos_comunes()
                                 + ["-J", "--no-playlist", "--no-warnings", "--", item["url"]])
                    salida, _ = p.communicate()
                    info = json.loads(salida[salida.find("{"):])
                    solo_audio = [f for f in info.get("formats", [])
                                  if f.get("acodec") not in (None, "none")
                                  and f.get("vcodec") in (None, "none")]
                    solo_audio.sort(key=lambda f: f.get("abr") or 0, reverse=True)
                    self.enviar("formatos", (item, solo_audio))
                    self.enviar("estado", "Listo.")
                    continue

                self.enviar("log", f"⬇ {item['titulo']}")
                self.enviar("progreso", 0)
                portadas = (self._portadas_ytmusic(item)
                            if opts["metadatos"] and opts["codec"] in CON_PORTADA else {})
                p = ejecutar(self._construir_comando(item, opts, format_id))
                for linea in p.stdout:
                    if linea.startswith("[FILE] "):
                        self._poner_portada_ytmusic(linea, portadas)
                    else:
                        self._leer_salida(linea)
                p.wait()
                self.enviar("progreso", 100 if p.returncode == 0 else 0)
                self.enviar("log", f"⚠ Terminado con errores: {item['titulo']}" if p.returncode
                            else f"✔ Terminado: {item['titulo']}")
            except Exception as e:
                self.enviar("log", f"✖ Error con «{item.get('titulo')}»: {e}")
            finally:
                if self.cola_descargas.empty():
                    self.enviar("estado", "Listo.")

    # ------------------------------------------------ carátulas de YT Music
    def _portadas_ytmusic(self, item):
        """{id_vídeo: url} con las carátulas cuadradas; la clave "*" vale para todas."""
        portadas = {}
        if item.get("portada"):
            clave = "*" if item["coleccion"] else id_de_video(item["url"]) or "*"
            portadas[clave] = item["portada"]
            if item["tipo"] == "album":
                return portadas
        if YTMusic is None:
            return portadas
        try:
            lista = id_de_lista(item["url"]) if item["coleccion"] else None
            if lista:
                pistas = self._get_ytm().get_playlist(lista, limit=None).get("tracks") or []
                for t in pistas:
                    url = portada_grande(t.get("thumbnails"))
                    if t.get("videoId") and url:
                        portadas[t["videoId"]] = url
            elif not item["coleccion"] and not portadas:
                vid = id_de_video(item["url"])
                if vid:
                    pistas = self._get_ytm().get_watch_playlist(videoId=vid, limit=1).get("tracks") or []
                    if pistas:
                        url = portada_grande(pistas[0].get("thumbnail") or pistas[0].get("thumbnails"))
                        if url:
                            portadas[vid] = url
        except Exception:
            pass  # sin carátula de YT Music: se queda la del vídeo
        return portadas

    def _poner_portada_ytmusic(self, linea, portadas):
        try:
            vid, ruta = linea.rstrip("\r\n")[7:].split(" ", 1)
        except ValueError:
            return
        url = portadas.get(vid) or portadas.get("*")
        if not url:
            return
        try:
            if incrustar_portada(ruta, descargar_imagen_jpeg(url)):
                self.enviar("log", f"[Carátula] Carátula de YouTube Music en «{Path(ruta).name}»")
        except Exception as e:
            self.enviar("log", f"⚠ No se pudo poner la carátula de YT Music ({e}); se deja la del vídeo.")

    # -------------------------------------------------------- actualizaciones
    def actualizar_ytdlp(self):
        if self.ytdlp is None:
            return
        threading.Thread(target=self._actualizar_hilo, args=(False,), daemon=True).start()

    def _actualizar_hilo(self, automatico):
        """Con yt-dlp.exe usa su autoactualización (-U); con pip, pip install -U."""
        es_exe = not (len(self.ytdlp) > 1 and self.ytdlp[1] == "-m")
        try:
            if es_exe:
                self.enviar("estado", "Buscando actualizaciones de yt-dlp…")
                p = ejecutar(self.ytdlp + ["-U"])
                salida, _ = p.communicate(timeout=180)
                ultima = [l for l in salida.splitlines() if l.strip()]
                texto = "\n".join(ultima)
                nueva = re.search(r"Updated yt-dlp to (\S+)", texto)
                al_dia = re.search(r"up to date \(([^ )]+)", texto)
                if nueva:
                    self.enviar("log", f"✔ yt-dlp actualizado a {nueva.group(1)}")
                elif al_dia:
                    self.enviar("log", f"yt-dlp al día ({al_dia.group(1)})")
                elif p.returncode:
                    self.enviar("log", "⚠ No se pudo actualizar yt-dlp: " + (ultima[-1] if ultima else "?"))
            elif not automatico:
                self.enviar("estado", "Actualizando yt-dlp…")
                r = subprocess.run([sys.executable, "-m", "pip", "install", "-U",
                                    "yt-dlp[default]", "ytmusicapi"], capture_output=True, text=True,
                                   creationflags=SIN_VENTANA)
                self.enviar("log", "✔ Actualizado. Reinicia la aplicación." if r.returncode == 0
                            else "✖ No se pudo actualizar: " + (r.stderr or "")[-300:])
            else:
                p = ejecutar(self.ytdlp + ["--version"])
                self.enviar("log", "yt-dlp " + p.communicate()[0].strip())
        except Exception as e:
            self.enviar("log", f"⚠ No se pudo comprobar la versión de yt-dlp: {e}")
        finally:
            self.enviar("estado", "Listo.")

    def _cerrar(self):
        self.cfg.update({
            "carpeta": self.var_carpeta.get(), "formato": self.var_formato.get(),
            "calidad": self.var_calidad.get(), "metadatos": self.var_meta.get(),
            "portada_cuadrada": self.var_cuadrada.get(), "tipo": self.var_tipo.get(),
        })
        try:
            CONFIG_PATH.write_text(json.dumps(self.cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        self.pool_miniaturas.shutdown(wait=False, cancel_futures=True)
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
