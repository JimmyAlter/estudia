# Cómo sumarte a Estudia

Gracias por pasar. Estudia lo hacemos entre estudiantes, y hay formas de ayudar que no requieren programar. Las dos que más nos sirven hoy son **sumar tu facultad** y **avisar cuando una pregunta está mal**.

Antes que nada, una regla que vale para todo el repo: **no subas material de ninguna cátedra.** Ni en issues, ni en pull requests, ni en capturas. Tampoco nombres de docentes o compañeros. El material es de tu cátedra; Estudia lo procesa solo en tu PC.

---

## 1. Sumar tu facultad al catálogo

Estudia se conecta a campus Moodle que tengan activo el servicio para celulares (el mismo que usa la app oficial de Moodle). Qué campus conoce lo dice un archivo de datos: `src/estudia/catalog/facultades.json`. Sumar una facultad es agregar una entrada ahí.

### Paso 1: fijate si tu campus sirve

Abrí esta dirección en el navegador, cambiando `<tu-campus>` por el dominio del campus de tu facultad:

```
https://<tu-campus>/lib/ajax/service-nologin.php?args=[{"index":0,"methodname":"tool_mobile_get_public_config","args":{}}]
```

Vas a ver un texto largo. Buscá tres datos:

- `"enablewebservices":1` y `"enablemobilewebservice":1`: si los dos están en 1, el campus sirve. Si alguno está en 0, o ves un error, por ahora no se puede.
- `"typeoflogin"`: cómo se inicia sesión.
  - `1` → usuario y contraseña (`"password"` en el catálogo)
  - `2` → por el navegador (`"browser"`)
  - `3` → embebido, por ejemplo con Office 365 (`"embedded"`)

### Paso 2: agregá la entrada

Este es el formato real que lee el código:

```json
{
  "id": "uba-cbc",
  "name": "UBA · Ciclo Básico Común",
  "university": "UBA",
  "url": "https://cbccampusvirtual.uba.ar",
  "login": "password"
}
```

- `id`: corto, en minúsculas, con guiones: universidad y facultad (`utn-frc`, `uba-sociales`).
- `name`: como lo reconocería un estudiante. Usá el formato `Universidad · Facultad`.
- `university`: `UTN` o `UBA` (o la que corresponda). Agrupa la lista en la pantalla de elegir facultad.
- `url`: la dirección base del campus, con `https://` y sin barra final.
- `login`: `"password"`, `"browser"` o `"embedded"`, según el paso 1.

### Importante: qué está probado hoy

Hoy el único inicio de sesión probado de punta a punta es **`"browser"`** (es el de UTN-FRBA). Los de usuario y contraseña (`"password"`) y embebido (`"embedded"`) llegan en la etapa 5 del roadmap.

Igual sirve muchísimo que sumes tu facultad ahora: nos ahorra el relevamiento, y cuando llegue la etapa 5 la probamos con tu entrada. En el pull request contá:

- el resultado del paso 1 (los tres valores, no hace falta pegar todo);
- si probaste iniciar sesión y qué pasó;
- si aparecieron tus materias y si se procesó alguna clase.

**¿No usás git?** Abrí un issue con la plantilla "Mi facultad no funciona" y poné la dirección del campus y el resultado del paso 1. Lo sumamos nosotros.

---

## 2. Reportar una pregunta incorrecta

Una pregunta con la respuesta mal es lo peor que le puede pasar a Estudia. Si encontrás una:

1. Tocá **"Reportar error en la pregunta"** adentro de la app. La saca de tu banco al toque.
2. Si podés, abrí un issue con la plantilla **"Pregunta incorrecta"**. Contá:
   - la materia y el tema;
   - qué decía la pregunta, **con tus palabras** (no la copies si sale de material de la cátedra);
   - qué marcó Estudia como correcta y por qué creés que está mal.

Con eso alcanza para revisar el prompt o la verificación. No adjuntes capturas de clases, PDFs ni diapositivas.

---

## 3. Código

### Levantar el entorno

Necesitás [uv](https://docs.astral.sh/uv/) y Node 22 o más nuevo.

```bash
uv sync
uv run pytest

cd web
npm ci
npm test
npm run build
cd ..
```

Para ver la app sin campus ni clave de Gemini, con datos de ejemplo:

```bash
uv run python scripts/dev_seed.py --port 8780
```

Abrí `http://127.0.0.1:8780/`. La base va a una carpeta temporal y los secretos a un keyring en memoria: no toca tus datos reales. Con `--step facultad|login|ia|listo|vencida` arrancás en otro punto del recorrido, y con `--no-exam` sin exámenes próximos.

### Reglas del proyecto

- **Solo lectura sobre el campus.** Nunca llamar funciones de Moodle que escriban (`*_save_*`, `*_submit_*`, `*_start_attempt`, `*_add_*`).
- **Secretos en el keyring** del sistema (servicio `estudia`). El token del campus y la clave de la IA nunca van a SQLite, a logs ni a mensajes de error.
- **El servidor escucha solo en `127.0.0.1`.**
- **Ninguna pregunta se muestra si no pasó la verificación.**
- **La interfaz está en castellano rioplatense**, sin emoji. La jerarquía la dan la tipografía y el espacio; íconos de línea (lucide-react) o ninguno.
- Pruebas para todo lo que cambie comportamiento.

### Por dónde empezar

Mirá los issues con la etiqueta [`para-empezar`](https://github.com/JimmyAlter/estudia/labels/para-empezar). Si querés encarar algo más grande, abrí antes una discusión en Ideas así no se pisa con lo que viene en el roadmap.

---

## Convivencia

Este proyecto sigue el [código de conducta](CODE_OF_CONDUCT.md). Para problemas de seguridad, no abras un issue: seguí [SECURITY.md](SECURITY.md).
