<div align="center">

# Estudia

**Practicá con las clases de tu campus y llegá al examen sabiendo qué te falta.**

Gratis, de código abierto y corre en tu PC. Hecho por un estudiante del ingreso a la UTN.

[![Licencia MIT](https://img.shields.io/badge/licencia-MIT-2a2c2e)](LICENSE)
[![Estado: etapa 1](https://img.shields.io/badge/estado-etapa%201%20·%20FRBA-c4532b)](#roadmap)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-2a2c2e)](https://www.python.org/)

<img src="docs/img/estudia-demo.gif" alt="Recorrido por Estudia: el inicio muestra el próximo examen y el tema más flojo; en la práctica se responde mal, aparece la explicación con el minuto de la clase, se responde bien, se ve el resultado y se abre un resumen en Materiales." width="860">

<sub>Recorrido real por la app, con datos de ejemplo de tres materias.</sub>

</div>

## Por qué

- **Practicás con el material de tu cátedra**, no con ejercicios sueltos de internet. Estudia toma las clases de tu campus Moodle y arma resúmenes y preguntas con corrección al instante.
- **Cuando le errás, te dice por qué y de dónde sale.** Cada respuesta trae la explicación, el error común y el minuto de la clase donde se ve. Lo que fallás vuelve hasta que te sale dos veces seguidas.
- **Sabés qué te falta para el próximo examen.** El inicio muestra cuánto falta, qué temas entran, cuánto dominás cada uno y cuál conviene practicar ahora.

## Cómo se ve

<table>
<tr>
<td width="50%">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/img/inicio-oscuro.png">
  <img src="docs/img/inicio-claro.png" alt="Inicio: Primer parcial de Química, faltan 12 días, los temas con su porcentaje y la sugerencia de practicar Uniones químicas.">
</picture>
</td>
<td width="50%">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/img/practica-oscuro.png">
  <img src="docs/img/practica-claro.png" alt="Práctica corregida: la respuesta elegida en rojo, la correcta en verde, la explicación, el error común y la clase con el minuto.">
</picture>
</td>
</tr>
</table>

Las capturas usan datos de ejemplo. Estudia no incluye material de ninguna cátedra.

## Cómo evita enseñarte algo mal

Una pregunta con la respuesta equivocada es peor que no tener pregunta. Por eso:

1. Las preguntas con cuentas se verifican con código antes de mostrarse.
2. Una segunda pasada de la IA las resuelve sin ver la respuesta. Si no coinciden, la pregunta se descarta.
3. Cada pregunta dice de qué clase y minuto sale.
4. Si igual encontrás una mal, el botón **"Reportar error en la pregunta"** la saca de tu banco. Y si nos contás cuál era, mejor (ver [Feedback](#feedback)).

## Privacidad, en criollo

- **Todo queda en tu compu.** Tus materias, los resúmenes, las preguntas y tus resultados se guardan en tu PC. No hay servidor de Estudia, no hay cuentas y no hay publicidad.
- **No ve tu contraseña del campus.** Iniciás sesión en la página de tu campus, como siempre, y Estudia recibe un permiso de solo lectura. Ese permiso y tu clave de Gemini se guardan en el almacén seguro de tu sistema (el mismo que usa el navegador para tus contraseñas).
- **Solo lee.** Nunca entrega, responde ni publica nada en tu campus.
- **Lo único que sale de tu PC** es lo que se le manda a Gemini para procesar cada clase.
- **Ojo con el plan gratis de Gemini.** Según los [términos de Google](https://ai.google.dev/gemini-api/terms), en el plan gratuito Google puede usar lo que recibe para mejorar sus productos, y puede leerlo una persona. Esos términos también piden **tener 18 años o más**. Si te importa la privacidad, usá una clave paga: ahí Google no usa tus datos para entrenar.
- **El material es de tu cátedra.** Estudia lo procesa solo para vos, con tu usuario. No lo publica, no lo comparte y no lo redistribuye. Por favor, no subas material de clases a los issues.

## Dónde funciona

Estudia se conecta a campus Moodle que tengan activo el servicio para celulares, el mismo que usa la app oficial de Moodle. Hoy anda de punta a punta con un solo campus; el resto ya está relevado y llega en la [etapa 5](#roadmap).

**UTN**

| Regional | Campus | Estado |
|---|---|---|
| Buenos Aires (FRBA) | aulasvirtuales.frba.utn.edu.ar | **Funciona hoy** |
| Córdoba | uv.frc.utn.edu.ar | Compatible, en camino |
| Rosario | moodle.frro.utn.edu.ar, frro.cvg.utn.edu.ar | Compatible, en camino |
| La Plata | frlp.cvg.utn.edu.ar | Compatible, en camino |
| Haedo | frh.cvg.utn.edu.ar | Compatible, en camino (login con Office 365) |
| Avellaneda | www.utnfravirtual.org.ar | Compatible, en camino |
| General Pacheco | frgp.cvg.utn.edu.ar | Compatible, en camino |
| Delta | frd.cvg.utn.edu.ar | Compatible, en camino |
| Santa Fe | campusvirtual.frsf.utn.edu.ar | Compatible, en camino |
| Tucumán | frt.cvg.utn.edu.ar | Compatible, en camino |
| Mendoza | campusvirtual.frm.utn.edu.ar | No por ahora: tiene apagado el servicio para celulares |

**UBA**

| Unidad | Campus | Estado |
|---|---|---|
| CBC | cbccampusvirtual.uba.ar | Compatible, en camino |
| UBA XXI | www.ubaxxicampusvirtual.uba.ar | Compatible, en camino (login con Microsoft 365) |
| Exactas | campus.exactas.uba.ar | Compatible, en camino (login con idEx) |
| Medicina | campus.fmed.uba.ar | Compatible, en camino |
| Sociales | campus.sociales.uba.ar | Compatible, en camino |
| Filosofía y Letras | campus.filo.uba.ar | Compatible, en camino |
| Agronomía | campus.agro.uba.ar | Compatible, en camino |
| Veterinaria | dogo.fvet.uba.ar | Compatible, en camino |
| Farmacia y Bioquímica | campus.ffyb.uba.ar | Compatible, en camino |
| Arquitectura (FADU) | campus.fadu.uba.ar | No por ahora: servicio para celulares apagado |
| Económicas | Moodle detrás de Office 365 | Sin verificar |
| Ingeniería (FIUBA) | campus.fi.uba.ar | No: el campus no es Moodle |
| Derecho | campus propio | No: el campus no es Moodle |
| Psicología, Odontología | — | Sin verificar |

"Compatible" quiere decir que el campus tiene el servicio activo; falta sumarlo al catálogo y probarlo. Si tu facultad no está, podés [sumarla vos](#sumá-tu-facultad).

## Instalación

**El instalador para Windows está en camino** ([etapa 6](#roadmap)). Mientras tanto, Estudia se corre desde el código. Son cinco comandos y no hace falta saber programar, pero sí usar una terminal.

**Hoy necesitás:**

- una cuenta en el campus de **UTN-FRBA** (las demás facultades llegan en la etapa 5);
- una clave gratis de Gemini, que se saca en [Google AI Studio](https://aistudio.google.com/apikey) (la app te guía);
- [uv](https://docs.astral.sh/uv/), que instala Python solo;
- [Node.js](https://nodejs.org/) 22 o más nuevo, para armar la interfaz una vez;
- git.

### 1. Instalá uv, Node y git

**Windows** (PowerShell):

```powershell
winget install --id=astral-sh.uv -e
winget install --id=OpenJS.NodeJS.LTS -e
winget install --id=Git.Git -e
```

**macOS** (con [Homebrew](https://brew.sh/)):

```bash
brew install uv node git
```

**Linux:**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# Node y git, con el gestor de paquetes de tu distribución
```

Cerrá y volvé a abrir la terminal para que tome los comandos nuevos.

### 2. Bajá Estudia y armá la interfaz

```bash
git clone https://github.com/JimmyAlter/estudia.git
cd estudia
uv sync
cd web && npm ci && npm run build && cd ..
```

### 3. Abrila

```bash
uv run python -m estudia
```

Se abre Estudia en tu navegador, en una página de tu propia PC (`127.0.0.1`). Elegís tu facultad, iniciás sesión en el campus y pegás tu clave de Gemini. Después procesa las clases de a una y te avisa cuando hay preguntas.

**Sobre el inicio de sesión:** al terminar el login, el campus te manda de vuelta con un enlace que empieza con `moodlemobile://`.

- **En Linux** Estudia lo registra solo y vuelve sin que hagas nada.
- **En Windows y macOS** todavía no (llega con el instalador). Si no vuelve solo, la pantalla de Estudia te deja pegar el enlace. En Chrome o Edge lo encontrás así: antes de iniciar sesión apretá F12 y abrí la pestaña **Consola**; al terminar aparece un aviso con la dirección `moodlemobile://token=…`. Copiala desde `moodlemobile://` hasta el final. Ese enlace es tu acceso: no se lo pases a nadie.

**Sobre el plan gratis de Gemini:** tiene un límite por día. Si se termina, Estudia lo dice y sigue con las clases que faltan al día siguiente.

### ¿Querés verla sin campus?

Con datos de ejemplo, sin tocar internet ni tus datos:

```bash
uv run python scripts/dev_seed.py --port 8780
```

y abrí `http://127.0.0.1:8780/`.

### Actualizar y desinstalar

- **Actualizar:** `git pull`, después `uv sync` y `cd web && npm ci && npm run build`.
- **Desinstalar:** borrá la carpeta `estudia` y la de datos:
  - Windows: `%LOCALAPPDATA%\estudia`
  - macOS: `~/Library/Application Support/estudia`
  - Linux: `~/.local/share/estudia` y `~/.local/share/applications/estudia-handler.desktop`

  El permiso del campus y la clave de Gemini quedan en el almacén seguro del sistema con el nombre `estudia`; los podés borrar desde ahí.

## Sumá tu facultad

Es la mejor primera contribución y **no hace falta programar**: el catálogo de campus es un archivo de datos, [`src/estudia/catalog/facultades.json`](src/estudia/catalog/facultades.json).

1. Fijate si tu campus sirve. Abrí esta dirección cambiando `<tu-campus>`:

   ```
   https://<tu-campus>/lib/ajax/service-nologin.php?args=[{"index":0,"methodname":"tool_mobile_get_public_config","args":{}}]
   ```

   Si ves `"enablewebservices":1` y `"enablemobilewebservice":1`, sirve. `"typeoflogin"` dice cómo se entra: `1` es usuario y contraseña, `2` es por navegador y `3` es embebido.

2. Sumá una entrada:

   ```json
   {
     "id": "uba-cbc",
     "name": "UBA · Ciclo Básico Común",
     "university": "UBA",
     "url": "https://cbccampusvirtual.uba.ar",
     "login": "password"
   }
   ```

   `login` va `"password"`, `"browser"` o `"embedded"`, según el `typeoflogin` del paso 1.

3. Mandá el pull request contando qué viste en el paso 1 y si pudiste iniciar sesión.

Hoy el único login probado de punta a punta es `"browser"`. Las entradas nuevas igual sirven: con ellas probamos la etapa 5. Si no usás git, abrí un issue de [**"Mi facultad no funciona"**](https://github.com/JimmyAlter/estudia/issues/new?template=facultad-no-funciona.yml) con la dirección del campus y lo sumamos nosotros. Más detalles en [CONTRIBUTING.md](CONTRIBUTING.md).

## Roadmap

- [x] **Etapa 1:** camino UTN-FRBA de punta a punta. Login por navegador, clases en video procesadas con Gemini, preguntas verificadas, práctica con repaso de errores, inicio centrado en el próximo examen y resúmenes.
- [ ] **Etapa 2:** simulacro con el reloj y la escala del examen real, y exámenes cargados a mano.
- [ ] **Etapa 3:** problemas en papel corregidos con una foto (desde la PC o el celular por QR), y métodos de la cátedra detectados y confirmados con vos.
- [ ] **Etapa 4:** Claude y ChatGPT además de Gemini, videos subidos a Moodle y PDFs.
- [ ] **Etapa 5:** catálogo completo (todas las facultades de la tabla), login con usuario y contraseña y con Office 365, y materias sin cuentas con respuestas verificadas contra una cita del material.
- [ ] **Etapa 6:** instalador para Windows, enlace de vuelta del login registrado solo y actualizaciones por GitHub Releases.

El orden puede cambiar según lo que pidan en [Ideas](https://github.com/JimmyAlter/estudia/discussions/categories/ideas).

## Feedback

Esto se hace entre estudiantes, así que lo que digas cambia lo que se construye.

- **Una pregunta con la respuesta mal:** tocá "Reportar error en la pregunta" en la app y, si podés, abrí un issue de [**"Pregunta incorrecta"**](https://github.com/JimmyAlter/estudia/issues/new?template=pregunta-incorrecta.yml). Contala con tus palabras, sin pegar material de la cátedra. Es el error que más nos importa.
- **Tu facultad no anda o no está:** issue de [**"Mi facultad no funciona"**](https://github.com/JimmyAlter/estudia/issues/new?template=facultad-no-funciona.yml).
- **Una idea o algo que te falta:** issue de [**"Idea"**](https://github.com/JimmyAlter/estudia/issues/new?template=idea.yml) o [Discussions](https://github.com/JimmyAlter/estudia/discussions). Votá con un pulgar las que ya están.
- **Preguntas, dudas de uso o charla:** [Discussions](https://github.com/JimmyAlter/estudia/discussions). Para todo lo demás, [abrí un issue](https://github.com/JimmyAlter/estudia/issues/new/choose).
- **Un problema de seguridad:** en privado, con el [reporte privado de vulnerabilidades](https://github.com/JimmyAlter/estudia/security/advisories/new) de GitHub, como dice [SECURITY.md](SECURITY.md). Nunca en un issue público.

Si Estudia te sirve, o te sirve la idea, dejale una estrella al repo. Nos ayuda a medir cuántos la esperan y a decidir qué facultad sumar primero.

## Preguntas frecuentes

**¿Es oficial de la UTN o de la UBA?** No. Es un proyecto independiente, hecho por estudiantes, sin relación con ninguna universidad.

**¿Cuánto cuesta?** Nada. La IA también es gratis con el plan gratuito de Gemini. Si usás una clave paga, pagás lo que cobre Google.

**¿Hace los cuestionarios por mí?** No. Solo lee el campus. No responde, no entrega y no toca nada.

**¿Anda en el celular?** No. Es para PC, porque procesar las clases necesita la compu prendida un rato.

**¿Por qué Gemini?** Porque tiene un plan gratis y puede mirar los videos de YouTube directamente, que es como viene el material de muchas cátedras. Claude y ChatGPT llegan en la etapa 4.

## Licencia

[MIT](LICENSE). Estudia no incluye ni distribuye material de ninguna cátedra. Los nombres UTN y UBA pertenecen a sus universidades y se usan solo para indicar con qué campus funciona.
