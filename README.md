# hass-intesisbox
Home Assistant IntesisBox Climate Platform

Note, this is very early stages of development and requires significant work.
Setting the mode and temperatures should work, but fan speeds and mode limits are not yet implemented.

### Usage
To use in your installation:
1. Download the intesisbox directory into your custom_components directory
2. Add the following lines to your `configuration.yaml` file:

```yaml
climate:
  - platform: intesisbox
    host: <IP Address>
```
