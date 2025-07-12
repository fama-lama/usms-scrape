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
| `SELENIUM_HOST`  | Hostname or IP where Selenium WebDriver (e.g., Selenium Grid or standalone) is running          | No       | `localhost`   |
| `SELENIUM_PORT`  | Port where Selenium WebDriver is listening                                                     | No       | `4444`        |
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
    image: famalama/usms-scrape:0.1
    environment:
      USMS_USERNAME: yourusername
      USMS_PASSWORD: yourpassword
      SELENIUM_HOST: selenium_host_or_ip
      SELENIUM_PORT: 4444
      MQTT_BROKER: mqtt_broker_ip
      MQTT_PORT: 1883
      MQTT_USERNAME: mqtt_user
      MQTT_PASSWORD: mqtt_password
      SCRAPE_INTERVAL: 1800
```


2. Start the container:

```docker-compose up -d```

The scraper will run automatically, scrape your USMS data every SCRAPE_INTERVAL seconds, and publish to MQTT if configured.

## Development

The main scraper script is usms.py.

Uses Selenium for browser automation and scraping. 

Requires Python dependencies listed in requirements.txt (including selenium and optionally paho-mqtt). 

Environment variables control behavior and credentials.

## License

This project is licensed under the GNU General Public License v3.0 (GPLv3). See LICENSE for details.
