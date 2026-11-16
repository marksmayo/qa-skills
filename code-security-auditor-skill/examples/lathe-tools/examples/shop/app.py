"""A deliberately weak sample shop the linter's documentation scans. Not part of the tools."""
import requests
from flask import Flask, render_template_string, request, send_file
from lxml import etree

app = Flask(__name__)
SECRET_KEY = 'shop-dev-key-1234'


@app.route('/download')
def download():
    return send_file(request.args['name'])


@app.route('/preview', methods=['POST'])
def preview():
    url = request.form['url']
    return requests.get(url, timeout=5).text


@app.route('/import', methods=['POST'])
def import_feed():
    doc = etree.fromstring(request.data)
    return str(len(doc))


@app.route('/hello')
def hello():
    return render_template_string('<p>{{ name|safe }}</p>', name=request.args.get('n', ''))
