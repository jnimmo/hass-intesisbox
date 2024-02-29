# hass-intesisbox

Home Assistant IntesisBox Climate Platform

This platform allows Home Assistant to control IntesisBox devices https://www.intesisbox.com/en/wifi/gateways/ using the WMP protocol.

Unlike IntesisHome, IntesisBox allows for control through the local network via TCP sockets which is better suited for using with home automation platforms.

This has only been tested with a device emulator to the specifications, please report any issues or create a pull request.

### Usage

To use in your installation:

1. Download the intesisbox directory into your custom_components directory
2. Add the  new "IntesisBox" integration in Home Assistant. Set as host the IntesisBox IP
