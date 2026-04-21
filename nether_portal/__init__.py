import subprocess
import os
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

from flask import Flask

from . import hevy, strava

subprocess.run(['git', '-C', os.environ['OBSIDIAN_VAULT_PATH'], 'pull', '--rebase'], check=True)

app = Flask(__name__)
app.register_blueprint(hevy.bp)
app.register_blueprint(strava.bp)
strava.start_subscription_thread()
