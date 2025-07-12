USMS Scraper
This project scrapes your USMS Smart Meter dashboard data using Selenium and optionally publishes updates to an MQTT broker.

Features
Logs into the USMS Smart Meter website

Scrapes remaining units, balance, and meter last polled time

Optionally publishes data to MQTT topics

Runs continuously at configurable intervals

Environment Variables
Variable	Description	Required	Default
USMS_USERNAME	Your USMS Smart Meter account username	Yes	N/A
USMS_PASSWORD	Your USMS Smart Meter account password	Yes	N/A
SELENIUM_HOST	Hostname or IP where Selenium WebDriver (e.g., Selenium Grid or standalone) is running	No	localhost
SELENIUM_PORT	Port where Selenium WebDriver is listening	No	4444
MQTT_BROKER	MQTT broker hostname or IP address	No	N/A
MQTT_PORT	MQTT broker port	No	N/A
MQTT_USERNAME	Username for MQTT broker authentication	No	N/A
MQTT_PASSWORD	Password for MQTT broker authentication	No	N/A
SCRAPE_INTERVAL	Time interval between scraping runs in seconds (integer)	No	1800 (30 min)

MQTT Topics Published
home/usms/remaining_unit — Remaining units on your smart meter

home/usms/remaining_balance — Remaining balance in your account

home/usms/meter_last_polled — Last meter polling timestamp

home/usms/last_run — Timestamp of the last scrape run

Usage
Prerequisites
Running Selenium WebDriver (e.g., Selenium Grid or standalone ChromeDriver) accessible by SELENIUM_HOST and SELENIUM_PORT

(Optional) MQTT broker accessible for publishing updates

Running with Docker Compose
Configure environment variables directly inside your docker-compose.yml under the service, for example:

yaml
Copy
Edit
services:
  usms-scrape:
    image: famalama/usms-scrape:0.1
    environment:
      - USMS_USERNAME=yourusername
      - USMS_PASSWORD=yourpassword
      - SELENIUM_HOST=selenium_host_or_ip
      - SELENIUM_PORT=4444
      - MQTT_BROKER=mqtt_broker_ip
      - MQTT_PORT=1883
      - MQTT_USERNAME=mqtt_user
      - MQTT_PASSWORD=mqtt_password
      - SCRAPE_INTERVAL=1800
Then simply run:

bash
Copy
Edit
docker-compose up -d
Development
Python script usms.py is the main scraper

Uses Selenium for web automation

Optionally publishes to MQTT if all MQTT env variables are set
