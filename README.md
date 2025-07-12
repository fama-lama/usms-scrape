# USMS Scraper

This project scrapes your USMS Smart Meter dashboard data using Selenium WebDriver and optionally publishes updates to an MQTT broker. It runs continuously at configurable intervals.

Only works for smart meters registered with https://www.usms.com.bn/smartmeter/index.html


---

## Features

- Logs into the USMS Smart Meter website  
- Scrapes remaining units, balance, and meter last polled time  
- Optionally publishes data to MQTT topics  
- Runs continuously with configurable scrape interval  

---

## Environment Variables

| Variable         | Description                                                                                     | Required | Default       |
|------------------|-------------------------------------------------------------------------------------------------|----------|---------------|
| `USMS_USERNAME`  | Your USMS Smart Meter account username                                                         | Yes      | N/A           |
| `USMS_PASSWORD`  | Your USMS Smart Meter account password                                                         | Yes      | N/A           |
| `SELENIUM_HOST`  | Hostname or IP where Selenium WebDriver (e.g., Selenium Grid or standalone) is running          | Yes       | `selenium`   |
| `SELENIUM_PORT`  | Port where Selenium WebDriver is listening                                                     | Yes       | `4444`        |
| `MQTT_BROKER`    | MQTT broker hostname or IP address                                                             | No       | N/A           |
| `MQTT_PORT`      | MQTT broker port                                                                               | No       | N/A           |
| `MQTT_USERNAME`  | Username for MQTT broker authentication                                                        | No       | N/A           |
| `MQTT_PASSWORD`  | Password for MQTT broker authentication                                                        | No       | N/A           |
| `SCRAPE_INTERVAL`| Time interval between scraping runs in seconds (integer)                                      | No       | `1800` (30 min)|

---

## MQTT Topics Published

- `home/usms/remaining_unit` — Remaining units on your smart meter  
- `home/usms/remaining_balance` — Remaining balance in your account  
- `home/usms/meter_last_polled` — Last meter polling timestamp  
- `home/usms/last_run` — Timestamp of the last scrape run  

---

## Usage

### Prerequisites

- Selenium WebDriver running and accessible (e.g., Selenium Grid or standalone ChromeDriver)  
- (Optional) MQTT broker accessible for publishing updates  

### Running with Docker Compose

1. Edit your `docker-compose.yml` to include:

```yaml
services:
  usms-scrape:
    image: famalama/usms-scrape:latest
    restart: unless-stopped
    container_name: usms-scrape
    environment:
      - USMS_USERNAME=01234567 #USMS account IC number 
      - USMS_PASSWORD=PASSWORD #USMS password
      - SELENIUM_HOST=selenium 
      - SELENIUM_PORT=4444 
      - MQTT_BROKER= #IP address of MQTT broker
      - MQTT_PORT= #Port of MQTT broker
      - MQTT_USERNAME= #MQTT username
      - MQTT_PASSWORD= #MQTT password
      - SCRAPE_INTERVAL=1800 #time in seconds between scrape
    depends_on:
      selenium:
        condition: service_healthy
  selenium:
    image: selenium/standalone-chrome:latest
    container_name: selenium
    healthcheck:
      test:
        - CMD
        - curl
        - -f
        - http://localhost:4444/wd/hub/status
      interval: 5s
      timeout: 2s
      retries: 10
    restart: unless-stopped
    ports:
      - 4444:4444
networks: {}

```


2. Start the container:

```docker-compose up -d```

The scraper will run automatically, scrape your USMS data every SCRAPE_INTERVAL seconds, and publish to MQTT if configured.

## Home Assistant sensors

insert this code into your configuration.yaml

```yaml
mqtt:
  sensor:
    - name: USMS Remaining Unit
      state_topic: home/usms/remaining_unit
      value_template: "{{ value.split(' ')[0] }}"
      unit_of_measurement: "kWh"
      state_class: total

    - name: USMS Remaining Balance
      state_topic: home/usms/remaining_balance
      value_template: "{{ value.replace('$', '') }}"
      unit_of_measurement: "BND"
      state_class: total
 
    - name: USMS Meter Last Polled
      state_topic: home/usms/meter_last_polled
      value_template: "{{ value }}"


    - name: USMS Last Run Time
      state_topic: home/usms/last_run
      value_template: "{{ value | as_datetime('%Y-%m-%d %H:%M:%S') }}"
```

## Development

The main scraper script is usms.py.

Uses Selenium for browser automation and scraping. 

Environment variables control behavior and credentials.

## License

This project is licensed under the GNU General Public License v3.0 (GPLv3). See LICENSE for details.
