"""Genera el dataset de demo para la tesis: 10 conversaciones simuladas del
agente de atención al cliente de SAMPLES (marca de ropa AR), inyectando
alucinaciones, errores de política, contradicciones, frustración y escalamiento.

Salida (en este mismo directorio):
  - samples_demo_tesis.csv        -> formato WATI ingestable (conversation_id,role,content,timestamp)
  - samples_demo_ground_truth.csv -> etiquetas de auditoría (qué debería detectar Insyta)

role: user = CLIENTE, assistant = AGENTE SAMPLES (el agente que Insyta evalúa).
Las alucinaciones SIEMPRE las comete el assistant.
Correr:  uv run python tests/fixtures/generate_samples_demo.py
"""

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

AR_TZ = timezone(timedelta(hours=-3))


def conv(cid, base, *turns):
    """turns: (role, content) ; timestamps se autoincrementan con gaps realistas."""
    rows = []
    t = base
    for role, content in turns:
        rows.append(
            {
                "conversation_id": cid,
                "role": role,
                "content": content,
                "timestamp": t.isoformat(),
            }
        )
        # gaps variados: cliente tarda más en responder que el bot
        t += timedelta(minutes=2 if role == "assistant" else 5)
    return rows


CONVERSATIONS = []
GROUND_TRUTH = []


def add(cid, base, turns, gt):
    CONVERSATIONS.extend(conv(cid, base, *turns))
    GROUND_TRUTH.append({"conversation_id": cid, **gt})


# ---------------------------------------------------------------------------
# 01 — Alucinación: códigos de descuento + envío gratis + reintegro inventados
add(
    "samples-demo-01",
    datetime(2025, 5, 3, 10, 12, tzinfo=AR_TZ),
    [
        ("user", "Hola! quería comprar el HOODIE NEO TECH BLACK, hay algún descuento?"),
        (
            "assistant",
            "Hola! Sí 😊 tenés un 25% OFF usando el código BIENVENIDO25 en el checkout, válido para tu primera compra.",
        ),
        ("user", "joya y el envío?"),
        ("assistant", "Con ese código el envío te queda bonificado a todo el país 🚚"),
        ("user", "perfecto pongo el código y no me toma el descuento"),
        (
            "assistant",
            "Raro! Probá con NEOTECH25 que es el que está activo esta semana",
        ),
        ("user", "tampoco anda, ninguno de los dos"),
        (
            "assistant",
            "No te preocupes, cualquier diferencia te la reintegramos después de la compra. Comprá tranquila que después lo ajustamos 🙌",
        ),
    ],
    {
        "tipo_problema": "alucinacion",
        "subtipo": "promo_codigo_inexistente",
        "severidad": "alta",
        "descripcion": "Inventa códigos BIENVENIDO25 y NEOTECH25, promete envío gratis y reintegro post-compra sin respaldo. Riesgo financiero y de confianza.",
        "mensaje_clave": "tenés un 25% OFF usando el código BIENVENIDO25",
    },
)

# 02 — Alucinación de stock + asesoramiento de talle contradictorio
add(
    "samples-demo-02",
    datetime(2025, 5, 8, 16, 40, tzinfo=AR_TZ),
    [
        ("user", "tenés la HOODIE NEO TECH GREY en talle XXL?"),
        ("assistant", "Sí! tenemos XXL en grey y en black, stock completo 👌"),
        ("user", "en la web solo me aparece hasta L"),
        (
            "assistant",
            "Ah esos son los cargados, pero en depósito tenemos hasta XXL. Lo cargamos y te lo enviamos sin problema",
        ),
        ("user", "dale entonces quiero un XXL grey"),
        (
            "assistant",
            "Igual te recomiendo pedir un L, nuestros talles vienen grandes y un L equivale a un XXL normal",
        ),
        ("user", "pero recién me dijiste que tenías XXL"),
        ("assistant", "Sí tenemos, pero por las dudas el L te va a quedar igual 😅"),
    ],
    {
        "tipo_problema": "alucinacion",
        "subtipo": "stock_inexistente",
        "severidad": "alta",
        "descripcion": "Afirma stock XXL que no existe en catálogo (web llega a L) y luego se contradice con el consejo de talle. Inconsistencia interna.",
        "mensaje_clave": "en depósito tenemos hasta XXL",
    },
)

# 03 — Política de devolución incorrecta (real: 30d cambio / 10d reintegro)
add(
    "samples-demo-03",
    datetime(2025, 5, 14, 11, 5, tzinfo=AR_TZ),
    [
        ("user", "hola compré hace 45 días y quería devolver, se puede?"),
        (
            "assistant",
            "Hola! Sí, tenés hasta 60 días corridos para devolución con reintegro de dinero, así que estás en tiempo 👍",
        ),
        ("user", "ah qué bueno, pensé que eran menos"),
        (
            "assistant",
            "No no, son 60 días tanto para cambio como para devolución, es la política nueva",
        ),
        ("user", "y cómo hago para el reintegro?"),
        (
            "assistant",
            "Pasame el CBU y te devolvemos el 100% incluido el envío original 🙌",
        ),
    ],
    {
        "tipo_problema": "error_politica",
        "subtipo": "plazo_devolucion_incorrecto",
        "severidad": "alta",
        "descripcion": "Política real: 30 días para cambio, 10 días para devolución con reintegro. El agente inventa 60 días para ambos y promete reintegrar el envío original. Compromiso financiero falso.",
        "mensaje_clave": "tenés hasta 60 días corridos para devolución con reintegro",
    },
)

# 04 — Alucinación: pedido despachado + tracking inventado, sin pago
add(
    "samples-demo-04",
    datetime(2025, 5, 20, 9, 30, tzinfo=AR_TZ),
    [
        (
            "user",
            "hola hice el pedido 18234 pero todavía no pagué, lo puedo pagar mañana?",
        ),
        (
            "assistant",
            "Hola! Tu pedido 18234 ya fue despachado hoy 🚚 número de seguimiento PC 99887766 1",
        ),
        ("user", "pero si no pagué todavía..."),
        ("assistant", "Está todo ok, sale en camino, en 48hs lo tenés"),
        ("user", "en serio? no pagué nada igual?"),
        ("assistant", "Sí sí, ya figura pago acá, no te preocupes"),
        ("user", "no me sacaron plata de la cuenta"),
        ("assistant", "Capaz tarda en impactar, pero el envío ya salió igual 📦"),
    ],
    {
        "tipo_problema": "alucinacion",
        "subtipo": "estado_pedido_falso",
        "severidad": "critica",
        "descripcion": "Inventa despacho y número de tracking de un pedido sin pagar, y confirma falsamente un pago inexistente. Riesgo operativo y financiero directo.",
        "mensaje_clave": "ya fue despachado hoy 🚚 número de seguimiento PC 99887766 1",
    },
)

# 05 — Frustración creciente / escalamiento, solo respuestas automáticas
add(
    "samples-demo-05",
    datetime(2025, 5, 26, 11, 0, tzinfo=AR_TZ),
    [
        ("user", "hola hace 12 días hice el pedido 17901 y no llega nada"),
        (
            "assistant",
            "Mensaje automático 🚨 Tu consulta será respondida de lunes a viernes de 9 a 16:30! Muchas gracias 😊",
        ),
        ("user", "es día de semana y son las 11 de la mañana"),
        (
            "assistant",
            "Mensaje automático 🚨 Tu consulta será respondida de lunes a viernes de 9 a 16:30! Muchas gracias 😊",
        ),
        ("user", "me están tomando el pelo? necesito una respuesta de una persona"),
        ("user", "pagué 130 lucas y nadie me responde hace una semana"),
        ("assistant", "Hola, lo derivamos al área de envíos"),
        ("user", "hace 3 días me dicen lo mismo, quiero la plata de vuelta"),
        ("assistant", "Entiendo, te responderán a la brevedad"),
        (
            "user",
            "voy a hacer la denuncia en defensa al consumidor, esto es una estafa",
        ),
    ],
    {
        "tipo_problema": "frustracion",
        "subtipo": "escalamiento_sin_resolucion",
        "severidad": "alta",
        "descripcion": "Cliente frustrado por demora de 12 días; el agente responde con mensajes automáticos repetidos sin resolver. Señal clara de escalamiento y churn (amenaza denuncia).",
        "mensaje_clave": "voy a hacer la denuncia en defensa al consumidor, esto es una estafa",
    },
)

# 06 — Contradicción sobre costo de envío (umbral cambia 3 veces)
add(
    "samples-demo-06",
    datetime(2025, 6, 2, 14, 20, tzinfo=AR_TZ),
    [
        ("user", "hola cuánto sale el envío a Córdoba capital?"),
        ("assistant", "Hola! El envío a Córdoba es gratis en compras desde $50.000 😊"),
        ("user", "mi compra es de $72.990 entonces es gratis"),
        ("assistant", "Mmm en realidad a Córdoba te sale $8.500 el envío"),
        ("user", "pero recién dijiste gratis desde 50 lucas"),
        ("assistant", "Sí, gratis desde $100.000, perdón"),
        ("user", "o sea cuál es? 50, 100 o 8.500?"),
        ("assistant", "Te lo dejo gratis igual, no te hagas problema"),
        ("user", "ok y cuánto tarda"),
        ("assistant", "24hs hábiles a todo el país 🚚"),
    ],
    {
        "tipo_problema": "contradiccion",
        "subtipo": "info_inconsistente",
        "severidad": "media",
        "descripcion": "Contradice el umbral de envío gratis tres veces ($50k -> $8.500 -> $100k -> gratis) e inventa entrega en 24hs a todo el país. Información poco confiable.",
        "mensaje_clave": "gratis desde $50.000 / te sale $8.500 / gratis desde $100.000",
    },
)

# 07 — Alucinación de precio (oferta inexistente más barata)
add(
    "samples-demo-07",
    datetime(2025, 6, 6, 17, 10, tzinfo=AR_TZ),
    [
        ("user", "cuánto está la BAGGY MARLON BLACK?"),
        ("assistant", "Hola! La Baggy Marlon Black está $45.990 😊"),
        ("user", "en la web me figura $72.990"),
        (
            "assistant",
            "No, ese es el precio viejo, ahora está de oferta a $45.990, aprovechá",
        ),
        ("user", "dale la quiero a 45.990 entonces"),
        ("assistant", "Perfecto, te la reservo a ese precio"),
        ("user", "me pasás el link de pago a 45.990?"),
        ("assistant", "Sí ahora te lo armo a $45.990 👌"),
    ],
    {
        "tipo_problema": "alucinacion",
        "subtipo": "precio_incorrecto",
        "severidad": "alta",
        "descripcion": "Inventa una oferta a $45.990 cuando el precio real es $72.990 y se compromete a respetarla. Pérdida directa o conflicto con el cliente.",
        "mensaje_clave": "ahora está de oferta a $45.990, aprovechá",
    },
)

# 08 — Datos de pago/reintegro erróneos
add(
    "samples-demo-08",
    datetime(2025, 6, 10, 12, 0, tzinfo=AR_TZ),
    [
        (
            "user",
            "hola quería el reintegro del SHORT TOKIO BLACK, ya devolví la prenda",
        ),
        (
            "assistant",
            "Hola! Listo, te transferimos los $66.990 más $5.000 de cortesía por la demora 🙌",
        ),
        ("user", "ah gracias, a qué cuenta me lo mandan?"),
        ("assistant", "Pasame tu alias"),
        ("user", "jose.crespo"),
        ("assistant", "Listo, transferido. Te llegó?"),
        ("user", "no me llegó nada"),
        ("assistant", "Fijate que lo mandamos al alias samples.mp, revisá ahí"),
        ("user", "ese es el alias de ustedes, no el mío"),
        ("assistant", "Cierto, ya sale de nuevo, en 72hs lo tenés seguro"),
    ],
    {
        "tipo_problema": "alucinacion",
        "subtipo": "reintegro_falso",
        "severidad": "critica",
        "descripcion": "Inventa un monto de cortesía de $5.000, afirma haber transferido cuando no lo hizo y envía al alias propio. Riesgo financiero y de confianza grave.",
        "mensaje_clave": "Listo, transferido. Te llegó? / lo mandamos al alias samples.mp",
    },
)

# 09 — Asesoramiento de talle confiadamente erróneo
add(
    "samples-demo-09",
    datetime(2025, 6, 14, 19, 25, tzinfo=AR_TZ),
    [
        ("user", "mido 1.85 y peso 90kg, qué talle de hoodie me va?"),
        (
            "assistant",
            "Hola! Con esa contextura un talle S te va perfecto, nuestros talles son grandes 👌",
        ),
        ("user", "S? no me parece muy chico?"),
        (
            "assistant",
            "Para nada, nuestro S equivale a un XL de otras marcas, está hecho oversize",
        ),
        ("user", "mmm no sé, no prefiero un L?"),
        ("assistant", "No, el L te va a quedar enorme, hacelo caso, pedí el S"),
        ("user", "bueno confío, pido el S"),
        ("assistant", "Vas a ver que te queda joya 😉"),
    ],
    {
        "tipo_problema": "alucinacion",
        "subtipo": "talle_equivalencia_falsa",
        "severidad": "media",
        "descripcion": "Afirma con seguridad una equivalencia de talles invertida (S = XL) que muy probablemente lleva a una compra equivocada y posterior cambio/devolución.",
        "mensaje_clave": "nuestro S equivale a un XL de otras marcas",
    },
)

# 10 — Promo/evento inventado (3x2) + ajuste manual falso
add(
    "samples-demo-10",
    datetime(2025, 6, 18, 13, 45, tzinfo=AR_TZ),
    [
        ("user", "hola es verdad que tienen 3x2?"),
        (
            "assistant",
            "Hola! Sí 😍 este finde tenemos 3x2 en toda la web, llevás 3 prendas y pagás 2",
        ),
        ("user", "en serio en todo? hoodies también?"),
        (
            "assistant",
            "Sí sí, en todo el catálogo incluido hoodies y baggies, hasta el domingo",
        ),
        ("user", "perfecto armo el carrito, cómo aplico el 3x2?"),
        ("assistant", "Se aplica solo en el carrito automáticamente"),
        ("user", "no se me aplica nada, sigo pagando las 3"),
        (
            "assistant",
            "Raro, debe ser que se cae el sistema por la demanda. Igual armá el pedido y te ajustamos el total a mano antes de cobrarte 🙌",
        ),
        ("user", "dale espero el ajuste entonces"),
    ],
    {
        "tipo_problema": "alucinacion",
        "subtipo": "promo_evento_inexistente",
        "severidad": "alta",
        "descripcion": "Inventa una promo 3x2 en toda la web, afirma aplicación automática y promete un ajuste manual del total. Genera expectativa imposible de cumplir.",
        "mensaje_clave": "este finde tenemos 3x2 en toda la web",
    },
)

# ---------------------------------------------------------------------------
HERE = Path(__file__).parent
conv_path = HERE / "samples_demo_tesis.csv"
gt_path = HERE / "samples_demo_ground_truth.csv"

with conv_path.open("w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(
        f, fieldnames=["conversation_id", "role", "content", "timestamp"]
    )
    w.writeheader()
    w.writerows(CONVERSATIONS)

with gt_path.open("w", newline="", encoding="utf-8") as f:
    fields = [
        "conversation_id",
        "tipo_problema",
        "subtipo",
        "severidad",
        "descripcion",
        "mensaje_clave",
    ]
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    w.writerows(GROUND_TRUTH)

n_conv = len({r["conversation_id"] for r in CONVERSATIONS})
print(f"OK -> {conv_path.name}: {n_conv} conversaciones, {len(CONVERSATIONS)} mensajes")
print(f"OK -> {gt_path.name}: {len(GROUND_TRUTH)} etiquetas de ground truth")
