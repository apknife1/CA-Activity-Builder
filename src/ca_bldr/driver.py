from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.options import Options
from .. import config  # adjust import if needed

def create_driver():
    options = Options()
    if config.HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--start-maximized")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()),
                              options=options)
    driver.implicitly_wait(3)  # we’ll still use explicit waits, but this helps
    return driver
