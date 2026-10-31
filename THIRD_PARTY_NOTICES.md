# Third-party notices

This repository redistributes the following third-party software. Python and
Node.js dependencies installed by `uv sync` / `npm ci` are not redistributed
here; their licenses are listed in their own packages.

## kawasemi/web/static/js/mermaid.min.js

Minified browser bundle of [Mermaid](https://github.com/mermaid-js/mermaid)
10.9.6, used to render plan diagrams in the web UI.

- Mermaid — MIT License, Copyright (c) 2014 - 2022 Knut Sveidqvist
- The bundle also contains, among others:
  - DOMPurify 3.4.2 — Apache License 2.0 or Mozilla Public License 2.0,
    (c) Cure53 and other contributors
  - js-yaml 4.1.0 — MIT License, Copyright (C) 2011-2015 by Vitaly Puzrin
  - KaTeX 0.16.45 — MIT License, Copyright (c) 2013-2020 Khan Academy and
    other contributors
  - Cytoscape.js 3.28.1 — MIT License, Copyright (c) 2016-2023 The Cytoscape
    Consortium
  - D3 modules — ISC License, Copyright 2010-2023 Mike Bostock
  - dagre / graphlib — MIT License, Copyright (c) 2012-2014 Chris Pettitt
  - lodash-es — MIT License, Copyright OpenJS Foundation and other contributors
  - Day.js — MIT License, Copyright (c) 2018-present iamkun
  - marked — MIT License, Copyright (c) 2018+ MarkedJS, Copyright (c) 2011-2018
    Christopher Jeffrey
  - stylis — MIT License, Copyright (c) 2016-present Sultan Tarimo
  - khroma — MIT License, Copyright (c) 2019-present Fabio Spampinato, Andrew Maney
  - Embedded in Cytoscape.js, with their banners kept in the bundle:
    - Thenable (Embeddable Minimum Strictly-Compliant Promises/A+ 1.1.1) —
      MIT License, Copyright (c) 2013-2014 Ralf S. Engelschall
    - Bezier curve function generator (bezier-easing) — MIT License,
      Copyright Gaetan Renaudeau
    - Runge-Kutta spring function generator, adapted from Framer.js — MIT
      License, Copyright Koen Bok
    - Event object based on jQuery events — MIT License, Copyright OpenJS
      Foundation and other contributors

The versions of Day.js, marked, stylis, khroma and the libraries embedded in
Cytoscape.js cannot be identified from the minified bundle, so none are given.

License banners embedded in the bundle are kept as distributed. The full
license texts are available from each project's repository.
