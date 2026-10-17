# Arquitectura de Estudia

Estudia es una app local: un servidor Python (FastAPI) que corre en tu PC, guarda todo en SQLite y sirve una interfaz React en el navegador. No hay servidor central ni cuentas. Lo único que sale de la PC son los pedidos al campus Moodle (solo lectura) y a Gemini.

```
campus Moodle ──(web service, solo lectura)──► sync ──► SQLite ◄── estudio (inicio, práctica)
                                                         ▲                  │
YouTube ──(enlace o video bajado)──► Gemini ──► tubería de generación       ▼
                                                 + verificación      API local ──► web (React)
```

## Módulos

Todo el código Python está en `src/estudia/`.

| Módulo | Qué hace |
|---|---|
| `__main__.py` | Arranque: elige un puerto libre en `127.0.0.1`, levanta uvicorn, abre el navegador y lanza la sincronización periódica. |
| `config.py` | Constantes (cadenas de modelos de Gemini, timeouts, cada cuánto sincronizar) y la carpeta de datos (`platformdirs`). |
| `db.py` | Esquema SQLite y migraciones: `site`, `course`, `section`, `material`, `topic`, `material_topic`, `exam`, `question`, `answer`, `kv`. |
| `secrets.py` | Token del campus y clave de IA en el keyring del sistema (servicio `estudia`). Nunca van a la base. |
| `catalog/` | `facultades.json`: los campus soportados (id, nombre, URL, tipo de login). |
| `moodle/auth.py` | Login por navegador: arma la URL de lanzamiento y lee el callback `moodlemobile://token=…`. |
| `moodle/client.py` | Cliente REST de solo lectura del web service de Moodle. Los errores se limpian de query strings para que el token no aparezca en mensajes. |
| `moodle/sync.py` | Sincronización incremental de cursos, secciones, materiales (pages con videos de YouTube) y cuestionarios. Todo o nada: si algo falla, se deshace. |
| `moodle/pages.py` | Saca los ids de YouTube del HTML de una page. |
| `material/video.py` | Baja un video con yt-dlp cuando Gemini no acepta el enlace directo. Valida el id antes de usarlo como nombre de archivo. |
| `ai/base.py` | Contrato del proveedor de IA, tipos (`VideoAnalysis`, `GenQuestion`) y errores (cuota, formato, auth, transitorio). |
| `ai/gemini.py` | Proveedor Gemini: análisis del video (resumen, temas, preguntas) y la pasada a ciegas. |
| `ai/rotation.py` | Rotación entre modelos: clasifica errores de Gemini y marca los modelos agotados hasta que vuelve la cuota diaria. |
| `ai/blind.py` | Pedido y lectura estricta de la pasada a ciegas. |
| `generation/pipeline.py` | Tubería por material: análisis → verificación → guardado de resumen, temas y preguntas. |
| `generation/verify.py` | Verificación matemática con SymPy en un proceso aparte, con lista blanca y tope de tiempo. |
| `generation/reverify.py` | Repite la pasada a ciegas sobre preguntas ya guardadas y retira las que no pasan. |
| `study/home.py` | Vista de inicio: próximo examen, días que faltan, temas con dominio, sugerencia y estado del procesamiento. |
| `study/mastery.py` | Dominio por tema: promedio ponderado de las últimas 20 respuestas, con vida media de 7 días. |
| `study/practice.py` | Arma cuestionarios, registra respuestas, repaso de errores y el botón "Reportar error en la pregunta". |
| `server/app.py` | API JSON (`/api/state`, `/api/home`, `/api/quiz`, `/api/answer`, …), tareas en segundo plano y protecciones del servidor local. |
| `server/handler.py` | Recibe `estudia://` y `moodlemobile://` del sistema y reenvía el callback al servidor local. En Linux se registra solo. |

La interfaz está en `web/` (React + Vite + TypeScript): pantallas de onboarding, inicio, práctica y materiales. `npm run build` la deja en `web/dist`, que sirve el mismo servidor.

## Flujo de datos

1. **Onboarding.** Elegís el campus del catálogo, iniciás sesión en el navegador y el callback vuelve por `server/handler.py`. El token va al keyring. Después pegás la clave de Gemini, que también va al keyring.
2. **Sincronización.** `sync_all` lee cursos, secciones visibles, pages y cuestionarios. Cada page con un video de YouTube se guarda como material `pending`; una page sin video queda `unsupported` y solo se vuelve a bajar si el campus dice que cambió. Los cuestionarios se guardan como exámenes con fecha de apertura, intentos, tiempo y nota máxima. Corre al abrir la app y cada 6 horas mientras está abierta.
3. **Generación.** `process_pending` toma los materiales pendientes de a uno. Gemini analiza el video (primero por enlace de YouTube; si no lo acepta, se baja con yt-dlp, se sube y se borra la copia local) y devuelve resumen, temas y preguntas con opción correcta, explicación, error común y minuto de la clase.
4. **Estudio.** El inicio muestra el próximo examen y el dominio de cada tema. La práctica arma cuestionarios con preguntas verificadas, prioriza lo que fallaste y el tema más flojo.

Si se agota la cuota de Gemini, el material queda pendiente y se sigue cuando vuelve. Las marcas de modelos agotados se guardan por clave para no repetir pedidos inútiles.

## Cómo se verifican las preguntas

Una pregunta solo se guarda como verificada si pasa todo esto:

1. **Chequeos locales, gratis.** Las opciones tienen que ser distintas. Si la pregunta trae una expresión (`check_expr`), SymPy confirma que vale lo mismo que la opción marcada y que ninguna otra opción coincide. La expresión viene del modelo, así que es entrada no confiable: se filtra con una lista blanca de caracteres y nombres, se acotan las potencias y se evalúa en un proceso aparte con tope de tiempo. Nunca se ejecuta código arbitrario.
2. **Pasada a ciegas.** Otro modelo, distinto del que hizo el análisis, recibe solo enunciados y opciones (sin respuesta ni explicación) y responde todas las preguntas de un material en un solo pedido. Cada respuesta tiene que traer el número de su pregunta; si vienen desordenadas, repetidas, de menos o de más, se descarta el lote entero. Si la respuesta a ciegas no coincide con la marcada, la pregunta se descarta.
3. **Reporte del estudiante.** Si igual se cuela una mal, "Reportar error en la pregunta" la retira y sus respuestas dejan de contar para el dominio. Se puede deshacer.

## Modelo de privacidad

- **Todo es local.** La base SQLite y los videos temporales viven en la carpeta de datos del usuario. No hay telemetría.
- **Secretos fuera de la base.** El token del campus y la clave de IA van al keyring del sistema. No aparecen en respuestas de la API, mensajes de error ni logs; los errores de red se limpian de query strings.
- **Servidor solo local.** Escucha en `127.0.0.1`. Un middleware de Host acepta solo nombres locales (contra DNS rebinding) y otro rechaza pedidos que cambian algo si vienen de otro origen (contra CSRF).
- **Campus en solo lectura.** El cliente usa solo funciones de lectura del web service. Estudia nunca entrega, responde ni publica nada.
- **El handler confirma el destino.** Antes de reenviar un token, `server/handler.py` comprueba que del otro lado esté Estudia y no otro proceso que haya tomado el puerto.
- **Lo que va a Gemini** es el video (o su enlace) y los enunciados para la pasada a ciegas. En el plan gratuito, Google puede usar ese contenido según sus términos; el README lo explica.

## Pruebas

- Python: `uv run pytest -q`. El campus y Gemini son dobles; los fixtures de `tests/fixtures/moodle/` son respuestas sintéticas con la misma forma que las de un Moodle real.
- Web: `cd web && npm ci && npm test && npm run build`.
- `scripts/dev_seed.py` levanta la app con datos de ejemplo, sin red ni keyring real.

## Roadmap

- **Etapa 1 (hecha):** UTN-FRBA de punta a punta. Login por navegador, videos procesados con Gemini, preguntas verificadas, práctica con repaso de errores, inicio centrado en el próximo examen y resúmenes.
- **Etapa 2:** simulacro con el reloj y la escala del examen real, y exámenes cargados a mano.
- **Etapa 3:** problemas en papel corregidos con una foto (desde la PC o el celular por QR), y métodos de la cátedra detectados y confirmados con el estudiante.
- **Etapa 4:** Claude y ChatGPT además de Gemini, videos subidos a Moodle y PDFs.
- **Etapa 5:** catálogo completo, login con usuario y contraseña y con Office 365, y materias sin cuentas con respuestas verificadas contra una cita del material.
- **Etapa 6:** instalador para Windows, enlace de vuelta del login registrado solo y actualizaciones por GitHub Releases.
