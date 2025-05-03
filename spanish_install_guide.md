# Guia instalación multicolor

El propósito de este documento es explicar el proceso completo de instalación de Klipper y Pico MMU en nuestra Ender 3 V3 SE para poder imprimir modelos de varios colores. Es necesario que cualquier persona pueda entenderlo correctamente así que si veis algo que se puede corregir o explicar mejor no dudéis en comentarlo o incluso proponer los cambios. Se trata de un documento colaborativo a través del cual todos podamos aprender.

Este mismo proceso es válido para la Ender 3 V3 KE.

# ¿Qué modificaciones necesito realizar?

 1. Klipper https://www.klipper3d.org/
 2. Cortador de filamento https://www.printables.com/model/1243521-filament-cutter-for-ender-3-v3-se
 3. Hub 4 lineas de filamento https://www.printables.com/model/1243385-pico-mmu-toolhead-filament-hub-for-ender-3-v3-se-4
 4. Pico MMU https://github.com/lhndo/LH-Stinger/wiki/Pico-MMU
 5. Soporte Pico: https://www.printables.com/model/1222596-pico-mmu-holder-for-ender-3-v3-ke

## Klipper

Para instalar Klipper en nuestra impresora es necesario disponer de un mini PC que sea capaz de ejecutar Linux, cualquier dispositivo que cumpla esto es válido. En este repositorio este proceso de instalación está automatizado para nuestra impresora, para ello solo hace falta ejecutar los siguientes comandos:

    git clone https://github.com/destaben/klipper_ender3_v3_se.git
    cd klipper_ender3_v3_se
    bash run_klipper.sh

Este proceso tardará alrededor de 5-10 minutos y generará como salida un fichero .bin cuya localización se muestra al terminar la ejecución del script.
Existen alternativas a este método automático usando KIAUH, aqui un tutorial: https://pblvsky.gitbook.io/ender3v3se/remote-control/klipper

Una vez tenemos generado el fichero .bin, sea cual sea el método, tenemos que flashear la impresora mediante la tarjeta SD. Para hacer esto, es necesario mover el archivo a la tarjeta SD y, con la impresora apagada, introducir la tarjeta SD.

Encendemos la impresora y la pantalla se quedará congelada. Es recomendable esperar unos minutos para asegurarnos que se ha hecho el flasheo. En caso de cualquier problema, es recomendable volver a probar (en mi caso funcionó a la segunda)

Posteriormente, deberíamos poder acceder a http://IP_DEL_MINI_PC y ya deberíamos de poder controlar la impresora.

## Cortador de filamento

Es una de las partes mas cruciales del multicolor, para que Pico MMU funcione correctamente el cortador tiene que estar entre el hotend y el extrusor. Para ello disponemos de este modelo, dejad un like al creador:   https://www.printables.com/model/1243521-filament-cutter-for-ender-3-v3-se

Es necesario que no exista rozamiento excesivo entre las piezas, recomiendo seguir las instrucciones al pie de la letra. Es muy importante lijar las piezas y aplicar lubricante a las partes móviles.

El cortador va colocado a presión entre el extrusor y el bloque del hotend, no es necesario atornillarlo aunque es posible. Este viene también con un tope que hay que instalar para que se produzca el corte. 
Para un mejor funcionamiento aconsejo poner un trocito de tubo PTFE tanto en el extrusor como en el propio hotend para facilitar la transición.

Una vez instalado, es necesario comprobar que el corte se realiza correctamente ejerciendo presión con la mano. El movimiento en ambos sentidos debe de ser suave y sin excesivo juego hacia los lados.

## Hub 4 lineas

Es otra parte muy importante de nuestro setup, recomiendo imprimirlo en 0,16 de altura o incluso menos. Las paredes y agujeros tienen que quedar lo mas perfectos posibles así que probablemente será necesario lijar o taladrar un poco los agujeros para los tubos. Reducir la velocidad de impresión también suele ayudar.

En mi caso también los he asegurado un poco de silicona caliente para evitar que los tubos se salgan por la presión.

## Pico MMU

Y por último tenemos el corazón del cambio de color, en este caso no voy a entrar en mucho detalle ya que todo esta perfectamente explicado aqui:

https://github.com/lhndo/LH-Stinger/wiki/Pico-MMU

Es importante seguir la documentación, cada punto es importante y tiene que ser leído. Revisad la lista de materiales primero para pedir lo que necesitéis. El resto de puntos están explicados ahí, tanto montaje como configuración o pequeños ajustes.

Cualquier duda podéis consultar conmigo o a través del Discord oficial del creador. https://discord.gg/G8bEaK5F

## Soporte Pico

Es un soporte para Pico MMU, se imprime y se sujeta a la máquina mediante un tornillo.