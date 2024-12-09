FROM ubuntu:18.04

RUN apt-get update && \
    apt-get install -y sudo git

# Create user
RUN useradd -ms /bin/bash klippy && adduser klippy dialout
USER klippy

#This fixes issues with the volume command setting wrong permissions
RUN mkdir /home/klippy/.config
VOLUME /home/klippy/.config

### Klipper setup ###
WORKDIR /home/klippy

USER root

# Clone the forked Klipper repository
ARG GITHUB_FORK_URL=https://github.com/0xD34D/klipper_ender3_v3_se.git
ARG GITHUB_BRANCH=master
RUN git clone --depth=1 --branch $GITHUB_BRANCH $GITHUB_FORK_URL /klipper

RUN echo 'klippy ALL=(ALL:ALL) NOPASSWD: ALL' > /etc/sudoers.d/klippy && \
    chown klippy:klippy -R /klipper
# This is to allow the install script to run without error
RUN ln -s /bin/true /bin/systemctl
USER klippy
RUN /klipper/scripts/install-ubuntu-18.04.sh
# Clean up install script workaround
RUN sudo rm -f /bin/systemctl

CMD ["/home/klippy/klippy-env/bin/python", "/klipper/klippy/klippy.py", "/home/klippy/.config/printer.cfg", "-a", "/home/klippy/.config/klippy_uds", "-l", "/opt/printer_data/logs/klippy.log"]
