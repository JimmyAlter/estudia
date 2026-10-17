# Seguridad

Estudia maneja cosas sensibles: el token de tu campus y tu clave de IA. Si encontrás un problema de seguridad, **no abras un issue público**. Avisanos en privado.

## Cómo reportar

1. **Por GitHub (preferido):** en la pestaña *Security* del repo, tocá **"Report a vulnerability"** ([enlace directo](https://github.com/JimmyAlter/estudia/security/advisories/new)). Queda privado entre vos y quien mantiene el proyecto.
2. **Si no podés usar ese formulario:** abrí un issue que diga solo "Necesito reportar un problema de seguridad", **sin detalles**, y te contactamos para seguir en privado por un aviso de seguridad del repo.

Contá qué encontraste, cómo reproducirlo y qué impacto creés que tiene. No hace falta que esté pulido. No incluyas tokens, claves ni material reales: si los necesitás para explicarlo, inventá unos de ejemplo.

## Qué cuenta como problema de seguridad

- Que el **token del campus** o la **clave de la IA** puedan leerse desde afuera de Estudia, terminen en la base SQLite, en logs, en mensajes de error o en respuestas de la API.
- Que el **servidor local** acepte conexiones desde otra máquina (tiene que escuchar solo en `127.0.0.1`).
- Que una página web cualquiera, abierta en tu navegador, pueda usar la API de Estudia: saltear la protección de **Host y Origin**, DNS rebinding, CSRF.
- Que el **handler de `estudia://` y `moodlemobile://`** acepte un token que no corresponde al inicio de sesión en curso, o se lo mande a un proceso que no es Estudia.
- Que Estudia **escriba en el campus** (entregue, responda o publique algo). Tiene que ser de solo lectura.
- Que mande a la IA datos de otras personas del campus.

Una pregunta con la respuesta mal no es un problema de seguridad: para eso está la plantilla "Pregunta incorrecta".

## Qué podés esperar

Estudia lo mantiene una sola persona que además estudia, así que los tiempos son estos, sin promesas de empresa:

- **Acuse de recibo:** dentro de los 7 días.
- **Primera evaluación:** dentro de los 14 días.
- **Arreglo:** lo antes posible según la gravedad. Si es grave, va antes que cualquier otra cosa del roadmap.

Cuando esté arreglado, publicamos un aviso y, si querés, te nombramos como quien lo encontró.

## Versiones

Solo se corrige la última versión publicada (hoy, la rama principal; el instalador todavía no salió).
