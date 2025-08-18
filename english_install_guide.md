<!-- filepath: [english_install_guide.md](http://_vscodecontentref_/0) -->
# Multicolor Installation Guide

The purpose of this document is to explain the complete process of installing Klipper and Pico MMU on our Ender 3 V3 SE to print multicolor models. Anyone should be able to understand it, so if you see something that can be improved or explained better, feel free to comment or propose changes. This is a collaborative document for everyone to learn.

This same process is valid for the Ender 3 V3 KE.

![General view](https://github.com/destaben/klipper_ender3_v3_se/blob/main/general_view.jpg)

# What modifications do I need?

 1. Klipper https://www.klipper3d.org/
 2. Filament cutter https://www.printables.com/model/1243521-filament-cutter-for-ender-3-v3-se
 3. 4-line filament hub https://www.printables.com/model/1243385-pico-mmu-toolhead-filament-hub-for-ender-3-v3-se-4
 4. Pico MMU https://github.com/lhndo/LH-Stinger/wiki/Pico-MMU
 5. Pico holder: https://www.printables.com/model/1222596-pico-mmu-holder-for-ender-3-v3-ke

## Klipper

To install Klipper and the environment on your printer using this repository, follow these steps:

1. Clone the repository and enter the folder:
    ```sh
    git clone https://github.com/destaben/klipper_ender3_v3_se.git
    cd klipper_ender3_v3_se
    ```

2. Start the automated installation with Docker Compose:
    ```sh
    docker compose up -d
    ```

3. To build the firmware, run the script:
    ```sh
    bash build_firmware.sh
    ```
   This will generate the `klipper.bin` file in the folder indicated at the end of the script.

4. Copy the `klipper.bin` file to an SD card and flash the printer (with the printer off, insert the SD and turn it on).

5. Access the web interface at http://MINI_PC_IP to control the printer.

Alternatively, you can use KIAUH following this tutorial: https://pblvsky.gitbook.io/ender3v3se/remote-control/klipper

## Filament Cutter

This is one of the most crucial parts for multicolor printing. For Pico MMU to work correctly, the cutter must be placed between the hotend and the extruder. Use this model and leave a like for the creator: https://www.printables.com/model/1243521-filament-cutter-for-ender-3-v3-se

Make sure there is no excessive friction between the parts; I recommend following the instructions closely. It is very important to sand the parts and apply lubricant to the moving parts.

The cutter is press-fitted between the extruder and the hotend block; screwing it in is optional. It also comes with a stopper that must be installed for the cut to occur.
For better performance, I recommend placing a small piece of PTFE tube in both the extruder and the hotend to facilitate filament transition.

Once installed, check that the cut is performed correctly by pressing manually. The movement in both directions should be smooth and without excessive play.

![PTFE Tube](https://github.com/destaben/klipper_ender3_v3_se/blob/main/PTFE_tube.jpg)

You will need to relocate the fan to the left side. There are many models available.

## 4-Line Hub

This is another very important part of our setup. I recommend printing it at 0.16 layer height or less. The walls and holes must be as perfect as possible, so you may need to sand or drill the holes for the tubes. Reducing print speed also helps.

In my case, I also secured the tubes with a bit of hot glue to prevent them from popping out due to pressure.

## Pico MMU

Finally, we have the heart of color changing. I won’t go into much detail here since everything is perfectly explained here:

https://github.com/lhndo/LH-Stinger/wiki/Pico-MMU

It is important to follow the documentation; every point is important and must be read. Check the materials list first to order what you need. The rest of the steps are explained there, including assembly, configuration, and fine-tuning.

If you have any questions, you can ask me or join the creator’s official Discord: https://discord.gg/G8bEaK5F

## Pico Holder

This is a holder for the Pico MMU; print it and attach it to the machine