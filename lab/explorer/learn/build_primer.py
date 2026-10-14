#!/usr/bin/env python3
"""Wrap primer.html (an Artifact page fragment) into a standalone page for the local lab server."""
import os
here = os.path.dirname(os.path.abspath(__file__))
frag = open(os.path.join(here, "primer.html"), encoding="utf-8").read()
os.makedirs(os.path.join(here, "primer"), exist_ok=True)
page = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
        '<style>:root{color-scheme:light;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}'
        'body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>\n</head>\n<body>\n' + frag + '\n</body>\n</html>\n')
open(os.path.join(here, "primer", "index.html"), "w", encoding="utf-8").write(page)
print("wrote primer/index.html", len(page), "bytes")
