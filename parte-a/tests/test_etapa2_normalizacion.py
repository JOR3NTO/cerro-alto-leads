# tests/test_etapa2_normalizacion.py — Pruebas de validación y normalización.
#
# IMPORTANTE: Los tests de normalizar_telefono fallarán con NotImplementedError
# hasta que escribas la función. Los de normalizar_email y validar_y_normalizar
# corren desde ya (cubren el código que yo escribí).

import pytest
from app.normalizacion import (
    normalizar_telefono,
    normalizar_email,
    validar_y_normalizar,
)


# ════════════════════════════════════════════════════════════════════════════════
# BLOQUE A — normalizar_email (código del mentor, deben pasar desde ya)
# ════════════════════════════════════════════════════════════════════════════════

class TestNormalizarEmail:

    def test_email_valido_limpio(self):
        email, invalido = normalizar_email("test@example.com")
        assert email == "test@example.com"
        assert invalido is False

    def test_email_con_mayusculas(self):
        """evento_03: 'Carla.Mendoza@example.com' → minúsculas"""
        email, invalido = normalizar_email("Carla.Mendoza@example.com")
        assert email == "carla.mendoza@example.com"
        assert invalido is False

    def test_email_con_espacios_alrededor(self):
        """evento_03: espacio al final del correo"""
        email, invalido = normalizar_email("  test@example.com  ")
        assert email == "test@example.com"
        assert invalido is False

    def test_email_sin_arroba(self):
        email, invalido = normalizar_email("noesuncorreo.com")
        assert email is None
        assert invalido is True

    def test_email_none(self):
        email, invalido = normalizar_email(None)
        assert email is None
        assert invalido is True

    def test_email_vacio(self):
        email, invalido = normalizar_email("")
        assert email is None
        assert invalido is True

    def test_email_spam(self):
        """evento_08: 'promo@ganadinero.example' — tiene formato válido aunque sea spam"""
        email, invalido = normalizar_email("promo@ganadinero.example")
        # El formato es técnicamente válido; el filtro de spam es responsabilidad del LLM
        assert invalido is False


# ════════════════════════════════════════════════════════════════════════════════
# BLOQUE B — normalizar_telefono (TU FUNCIÓN — estos tests deben pasar cuando
#             implementes la función en normalizacion.py)
# ════════════════════════════════════════════════════════════════════════════════

class TestNormalizarTelefono:

    def test_telefono_ec_sin_codigo(self):
        """evento_01: '0991234567' → '+593991234567' (región EC por defecto)"""
        tel, invalido = normalizar_telefono("0991234567")
        assert tel == "+593991234567"
        assert invalido is False

    def test_telefono_ec_con_espacios(self):
        """evento_03: '+593 99 123 4567' con espacios"""
        tel, invalido = normalizar_telefono("+593 99 123 4567")
        assert tel == "+593991234567"
        assert invalido is False

    def test_telefono_ec_formato_local_espacios(self):
        """evento_07: '098 765 4321' → '+593987654321'"""
        tel, invalido = normalizar_telefono("098 765 4321")
        assert tel == "+593987654321"
        assert invalido is False

    def test_telefono_extranjero_valido(self):
        """evento_04: '+1 (305) 555-0142' — válido aunque no sea EC"""
        tel, invalido = normalizar_telefono("+1 (305) 555-0142")
        assert tel == "+13055550142"
        assert invalido is False

    def test_telefono_claramente_falso(self):
        """evento_08: '1111111111' — parseble pero no válido en ningún país"""
        tel, invalido = normalizar_telefono("1111111111")
        assert tel is None
        assert invalido is True

    def test_telefono_none(self):
        """evento_05: campo phone ausente → None"""
        tel, invalido = normalizar_telefono(None)
        assert tel is None
        assert invalido is True

    def test_telefono_vacio(self):
        tel, invalido = normalizar_telefono("")
        assert tel is None
        assert invalido is True

    def test_telefono_texto_puro(self):
        tel, invalido = normalizar_telefono("no soy un telefono")
        assert tel is None
        assert invalido is True

    def test_telefono_ec_con_guiones(self):
        """Formato '0987-111-222' — variante con guiones"""
        tel, invalido = normalizar_telefono("0987-111-222")
        assert tel == "+593987111222"
        assert invalido is False


# ════════════════════════════════════════════════════════════════════════════════
# BLOQUE C — validar_y_normalizar (orquestador — depende de tu función)
# ════════════════════════════════════════════════════════════════════════════════

class TestValidarYNormalizar:

    def test_ambos_validos(self):
        r = validar_y_normalizar("0991234567", "test@example.com")
        assert r.telefono_e164 == "+593991234567"
        assert r.email_normalizado == "test@example.com"
        assert r.telefono_invalido is False
        assert r.email_invalido is False
        assert r.debe_rechazarse is False

    def test_solo_telefono_valido(self):
        """evento_05 al revés: correo inválido, teléfono válido"""
        r = validar_y_normalizar("0991234567", "no-es-correo")
        assert r.telefono_e164 == "+593991234567"
        assert r.email_normalizado is None
        assert r.email_invalido is True
        assert r.debe_rechazarse is False

    def test_solo_email_valido(self):
        """evento_05: sin campo phone, correo válido"""
        r = validar_y_normalizar(None, "test@example.com")
        assert r.telefono_e164 is None
        assert r.telefono_invalido is True
        assert r.email_normalizado == "test@example.com"
        assert r.debe_rechazarse is False

    def test_ambos_invalidos_debe_rechazarse(self):
        """Sin ningún medio de contacto → rechazado con motivo"""
        r = validar_y_normalizar("1111111111", "notanemail")
        assert r.debe_rechazarse is True
        assert r.motivo_rechazo is not None
        assert len(r.motivo_rechazo) > 0

    def test_nombre_vacio_no_descarta(self):
        """evento_05: nombre vacío no impide procesar el lead"""
        # El nombre no entra en validar_y_normalizar, pero documentamos
        # que la decisión de no rechazar por nombre vacío es CORRECTA.
        r = validar_y_normalizar(None, "sin_nombre@example.com")
        assert r.debe_rechazarse is False
