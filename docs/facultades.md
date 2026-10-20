# Facultades y primer arranque en Windows

### Facultades que funcionan

Revisá esto antes de bajar el instalador.

| Facultad | Estado |
|---|---|
| UTN · Facultad Regional Buenos Aires (FRBA) | **Funciona** |
| Todas las demás | Todavía no. Anotate en la lista de espera con un issue de [**"Mi facultad no funciona"**](https://github.com/JimmyAlter/estudia/issues/new?template=facultad-no-funciona.yml). |

### Si Windows dice "Windows protegió su PC"

Es el filtro SmartScreen de Windows. Para seguir:

1. En ese cartel, tocá **Más información**.
2. Abajo aparece el botón **Ejecutar de todas formas**. Tocalo.
3. La instalación sigue como siempre.

Aparece porque el instalador no está firmado con un certificado de pago, no porque se haya encontrado algo malo. El código de Estudia es abierto y lo podés revisar en este mismo repositorio, y este instalador lo arma GitHub a partir de ese código. Es probable que el aviso siga apareciendo durante las primeras semanas de cada versión, hasta que Windows la vea en más computadoras.

Cada versión publica el SHA-256 del instalador en sus notas, para que puedas comprobarlo.
