from flask import Flask

from . import hevy, strava

app = Flask(__name__)
app.register_blueprint(hevy.bp)
app.register_blueprint(strava.bp)
strava.start_subscription_thread()
