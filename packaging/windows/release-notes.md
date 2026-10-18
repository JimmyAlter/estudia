### Facultades que funcionan

Revisá esto antes de bajar el instalador.

| Facultad | Estado |
|---|---|
| UTN · Facultad Regional Buenos Aires (FRBA) | **Funciona** |
| Todas las demás | Todavía no. Anotate en la lista de espera con un issue de [**"Mi facultad no funciona"**](https://github.com/JimmyAlter/estudia/issues/new?template=facultad-no-funciona.yml). |

## Novedades de la 0.5.0

- **Materia de ejemplo:** en la primera pantalla podés probar Estudia con una materia de matemática y un parcial de ejemplo, sin campus, sin clave de Gemini y sin internet. Las preguntas están escritas para Estudia y no se mezclan con tus materias reales.
- **Asistente para la clave de Gemini:** te lleva a Google AI Studio, te dice qué tocar y verifica la clave apenas la pegás. Si Google la rechaza más adelante, te pide una nueva.
- **Variantes numéricas:** una pregunta con cuentas puede volver con otros números, así practicás el método y no te acordás el resultado. Cada variante se verifica con SymPy antes de mostrarse; si hay alguna duda, se descarta.
- **Instalador más fácil de comprobar:** estas notas traen el SHA-256 del instalador y los pasos de SmartScreen uno por uno.

Si ya tenías Estudia, instalá encima: tus materias y tu progreso se mantienen.

## Cómo instalar Estudia en Windows

1. Bajá `EstudiaSetup-{{VERSION}}.exe` de la lista de abajo (Assets).
2. Abrilo con doble clic y seguí los pasos. No pide permisos de administrador: se instala solo para tu usuario.
3. Al terminar, Estudia se abre en tu navegador. También la encontrás en el menú Inicio.

### Si Windows dice "Windows protegió su PC"

Es el filtro SmartScreen de Windows. Para seguir:

1. En ese cartel, tocá **Más información**.
2. Abajo aparece el botón **Ejecutar de todas formas**. Tocalo.
3. La instalación sigue como siempre.

Aparece porque el instalador no está firmado con un certificado de pago, no porque se haya encontrado algo malo. El código de Estudia es abierto y lo podés revisar en este mismo repositorio, y este instalador lo arma GitHub a partir de ese código. Es probable que el aviso siga apareciendo durante las primeras semanas de cada versión, hasta que Windows la vea en más computadoras.

### Comprobar el instalador (opcional)

El SHA-256 de `EstudiaSetup-{{VERSION}}.exe` es:

```
{{SHA256}}
```

Lo calcula GitHub al publicar. Para compararlo, abrí PowerShell en la carpeta de Descargas y escribí `Get-FileHash .\EstudiaSetup-{{VERSION}}.exe -Algorithm SHA256`. Las dos cadenas tienen que ser iguales (no importan las mayúsculas).

### Actualizar y desinstalar

- **Actualizar:** bajá el instalador nuevo y abrilo. Tus materias y tu progreso se mantienen.
- **Desinstalar:** desde Configuración → Aplicaciones → Estudia. Te pregunta si querés borrar también tus datos de estudio; si decís que no, quedan por si la volvés a instalar.
