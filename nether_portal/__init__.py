from flask import Flask

from . import hevy

app = Flask(__name__)
app.register_blueprint(hevy.bp)

if __name__ == '__main__':
    app.run(debug=True, port=5000)
