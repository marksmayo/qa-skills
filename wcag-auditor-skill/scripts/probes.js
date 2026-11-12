// In-page helpers for run_audit.py. Injected after the page loads; defines window.__qa.
// Every function only reads the page except textSpacing(), which adds a style element, and
// focusStart(), which adds an invisible focus starting point; run_audit.py removes both again.
// Nothing here submits forms, clicks or follows links.
(() => {
  if (window.__qa) return;

  const INTERACTIVE = 'a[href],area[href],button,input:not([type=hidden]),select,textarea,summary,iframe,' +
    '[tabindex],[contenteditable=""],[contenteditable=true],[role=button],[role=link],[role=checkbox],' +
    '[role=radio],[role=switch],[role=tab],[role=menuitem],[role=option],[role=slider],[role=spinbutton],' +
    '[role=combobox],[role=textbox],[role=treeitem],[role=gridcell]';
  const EXEMPT_REFLOW = 'table,pre,code,canvas,svg,video,iframe,img,[role=grid],[role=table],[data-reflow-exempt]';
  const LANDMARK = 'header,nav,main,footer,aside,form,section[aria-label],section[aria-labelledby],' +
    '[role=banner],[role=navigation],[role=main],[role=contentinfo],[role=complementary],[role=search],[role=form],[role=region],[role=dialog]';

  function xpath(el) {
    if (!el || el.nodeType !== 1) return null;
    const parts = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      let i = 1;
      for (let s = n.previousElementSibling; s; s = s.previousElementSibling) if (s.localName === n.localName) i++;
      parts.unshift(`${n.localName}[${i}]`);
      if (!n.parentElement && n.getRootNode() instanceof ShadowRoot) {
        const host = xpath(n.getRootNode().host);
        return host + '/#shadow-root/' + parts.join('/');
      }
    }
    return '/' + parts.join('/');
  }

  function stableClasses(el) {
    return [...el.classList].filter(c => !/\d{3,}|^(is|has)-|active|open|focus|hover|selected|current|^(invalid|valid|error|visible|hidden|show|shown|in)$|css-|sc-|jsx-/i.test(c)).slice(0, 2);
  }

  function landmarkOf(el) {
    const l = el.closest && el.closest(LANDMARK);
    if (!l) return 'body';
    return l.getAttribute('role') || l.localName;
  }

  // Component signature: same component on different pages gets the same signature.
  function sig(el) {
    if (!el || el.nodeType !== 1) return null;
    const own = el.localName + stableClasses(el).map(c => '.' + c).join('');
    const p = el.parentElement;
    const parent = p ? p.localName + stableClasses(p).map(c => '.' + c).join('') : '';
    const id = el.id && !/\d{3,}/.test(el.id) ? '#' + el.id : '';
    return `${landmarkOf(el)} ${parent} > ${own}${id}`.replace(/\s+/g, ' ').trim();
  }

  function cssPath(el) {
    if (!el || el.nodeType !== 1) return null;
    if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) return '#' + CSS.escape(el.id);
    const parts = [];
    for (let n = el; n && n.nodeType === 1 && n !== document.documentElement; n = n.parentElement) {
      let s = n.localName;
      if (n.id && document.querySelectorAll('#' + CSS.escape(n.id)).length === 1) { parts.unshift('#' + CSS.escape(n.id)); break; }
      const sib = n.parentElement ? [...n.parentElement.children].filter(c => c.localName === n.localName) : [];
      if (sib.length > 1) s += `:nth-of-type(${sib.indexOf(n) + 1})`;
      parts.unshift(s);
    }
    return parts.join(' > ');
  }

  function snippet(el) {
    if (!el || el.nodeType !== 1) return null;
    const open = el.outerHTML.match(/^<[^>]*>/);
    let s = open ? open[0] : el.outerHTML.slice(0, 200);
    const text = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
    if (text && !['html', 'body', 'main', 'header', 'footer', 'nav', 'form', 'section', 'div'].includes(el.localName))
      s += text.slice(0, 60) + (text.length > 60 ? '…' : '');
    return s.length > 300 ? s.slice(0, 300) + '…' : s;
  }

  function rect(el) {
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) };
  }

  function visible(el) {
    if (!el || el.nodeType !== 1) return false;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || Number(cs.opacity) === 0) return false;
    return !el.closest('[hidden],[inert]');
  }

  // Visually hidden but exposed to assistive technology (.sr-only, .visually-hidden): clipped to
  // nothing with clip or clip-path, or a 1 x 1 px box with overflow hidden, on the element or an
  // ancestor. Such text is never seen, so it cannot be "clipped" or low-contrast for a reader.
  function visuallyHidden(el) {
    for (let n = el; n && n.nodeType === 1 && n !== document.body && n !== document.documentElement; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.clip && cs.clip.startsWith('rect') && ['absolute', 'fixed'].includes(cs.position)) {
        const v = (cs.clip.match(/-?[\d.]+/g) || []).map(Number);
        if (v.length === 4 && v[1] - v[3] <= 1 && v[2] - v[0] <= 1) return true;
      }
      if (/^inset\(\s*(50|100)%/.test(cs.clipPath || '')) return true;
      const r = n.getBoundingClientRect();
      if (r.width <= 1 && r.height <= 1 && [cs.overflowX, cs.overflowY].some(o => o === 'hidden' || o === 'clip')) return true;
    }
    return false;
  }

  // What lies behind an element's text, for contrast results the engines could not compute. Walking
  // up from the text: translucent background colours on the way (overlays, nearest first), the
  // nearest background image (a gradient keeps its colour stops) and the element carrying it, and
  // the first opaque background colour, which is either in front of the image (opaque_first: the
  // image is not behind the text) or beneath it. Plus the text colour and size.
  // The alpha of a computed colour in any syntax: rgba(r, g, b, a), or "/ a" in the space-separated and
  // CSS Color 4 forms (lab(), oklch(), color()); 1 when there is none, 0 for transparent.
  function alphaOf(c) {
    if (!c || c === 'transparent') return 0;
    let m = c.match(/\/\s*([\d.]+)(%?)\s*\)\s*$/);
    if (m) return Number(m[1]) / (m[2] ? 100 : 1);
    m = c.match(/^rgba\(\s*[^,]+,[^,]+,[^,]+,\s*([\d.]+)(%?)\s*\)$/);
    if (m) return Number(m[1]) / (m[2] ? 100 : 1);
    return 1;
  }

  // An <img>, <video>, <canvas>, <picture> or <svg> painted behind the text (not inside it): positioned
  // hero media, a video poster. Its pixels are the real background, so no colour estimate is possible.
  function mediaBehind(el) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || !document.elementsFromPoint) return null;
    const x = Math.min(Math.max(r.left + r.width / 2, 0), innerWidth - 1), y = Math.min(Math.max(r.top + r.height / 2, 0), innerHeight - 1);
    if (r.left + r.width / 2 < 0 || r.top + r.height / 2 < 0 || r.left + r.width / 2 >= innerWidth || r.top + r.height / 2 >= innerHeight) return null;
    const stack = document.elementsFromPoint(x, y);
    const at = stack.findIndex(n => n === el || el.contains(n));
    for (const n of stack.slice(at < 0 ? 0 : at + 1)) {
      if (el.contains(n) || n.contains(el)) continue;
      if (n.matches('img,video,canvas,picture,svg,object,embed')) return { tag: n.localName, xpath: xpath(n), selector: cssPath(n) };
    }
    return null;
  }

  function contrastContext(el) {
    if (!el || el.nodeType !== 1) return null;
    const cs = getComputedStyle(el);
    let imgOn = null, image = null, bgColor = null, opaqueFirst = false;
    const overlays = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const s = getComputedStyle(n);
      const alpha = alphaOf(s.backgroundColor);
      if (!imgOn && s.backgroundImage && s.backgroundImage !== 'none') { imgOn = n; image = s.backgroundImage.slice(0, 300); }
      if (alpha >= 1) { bgColor = s.backgroundColor; opaqueFirst = !imgOn; break; }
      if (alpha > 0 && !imgOn) overlays.push(s.backgroundColor);  // translucent layers in front of the image
    }
    let media = null;
    try { media = mediaBehind(el); } catch (e) { media = null; }
    return { fg: cs.color, font_px: parseFloat(cs.fontSize), font_weight: Number(cs.fontWeight) || 400,
             bg_image_on: imgOn ? { xpath: xpath(imgOn), sig: sig(imgOn), selector: cssPath(imgOn) } : null,
             bg_image: image, bg_color: bgColor || 'rgb(255, 255, 255)', opaque_first: opaqueFirst, overlays: overlays.slice(0, 6),
             media_behind: media, visually_hidden: visuallyHidden(el) };
  }

  function describe(el) {
    return el && el.nodeType === 1 ? { xpath: xpath(el), selector: cssPath(el), sig: sig(el), html: snippet(el), rect: rect(el) } : null;
  }

  function byXpath(xp) {
    if (!xp) return null;
    if (xp.includes('#shadow-root')) {
      const [host, rest] = xp.split('/#shadow-root/');
      const h = byXpath(host);
      if (!h || !h.shadowRoot) return null;
      let n = h.shadowRoot;
      for (const step of rest.split('/')) {
        const m = step.match(/^([^\[]+)\[(\d+)\]$/);
        if (!m) return null;
        n = [...n.children].filter(c => c.localName === m[1])[Number(m[2]) - 1];
        if (!n) return null;
      }
      return n;
    }
    try { return document.evaluate(xp, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue; }
    catch (e) { return null; }
  }

  function bySelector(target) {
    // axe targets: ['sel'] or [['host', 'inner']] for shadow DOM; frames are not resolved here.
    try {
      let root = document, el = null;
      const chain = Array.isArray(target[0]) ? target[0] : [target[0]];
      for (const sel of chain) {
        el = root.querySelector(sel);
        if (!el) return null;
        root = el.shadowRoot || el;
      }
      return target.length > 1 ? null : el;
    } catch (e) { return null; }
  }

  function deepActive() {
    let a = document.activeElement;
    while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
    return a;
  }

  function isFocusable(el) {
    if (el.disabled) return false;
    if (el.closest('[inert]')) return false;
    const ti = el.getAttribute('tabindex');
    if (ti !== null && Number(ti) < 0) return false;
    return el.tabIndex >= 0;
  }

  function interactive() {
    return [...document.querySelectorAll(INTERACTIVE)].filter(visible);
  }

  // Elements that look clickable (pointer cursor, onclick) but are not focusable or native controls.
  function pointerOnly() {
    const out = [];
    for (const el of document.querySelectorAll('body *')) {
      if (!visible(el) || el.matches(INTERACTIVE) || el.closest('a[href],button,label,summary')) continue;
      const cs = getComputedStyle(el);
      const parentPointer = el.parentElement && getComputedStyle(el.parentElement).cursor === 'pointer';
      if ((cs.cursor === 'pointer' && !parentPointer) || el.hasAttribute('onclick')) out.push(el);
    }
    return out.slice(0, 200);
  }

  function landmarks() {
    return [...document.querySelectorAll(LANDMARK)].filter(visible).map(l => ({
      role: l.getAttribute('role') || l.localName, label: l.getAttribute('aria-label') || null, xpath: xpath(l) }));
  }

  function headings() {
    return [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role=heading]')].filter(visible).map(h => ({
      level: h.getAttribute('aria-level') ? Number(h.getAttribute('aria-level')) : Number(h.localName[1]) || null,
      text: (h.innerText || '').trim().slice(0, 120), xpath: xpath(h) }));
  }

  function template() {
    const lm = landmarks().map(l => l.role).join('+');
    const main = document.querySelector('main,[role=main]') || document.body;
    const shape = [...main.children].slice(0, 6).map(c => c.localName).join(',');
    return { landmarks: lm, main_shape: shape };
  }

  // Focus indicator obstruction: sample 9 points of the focused element.
  function obscured(el) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return { covered: 0, total: 0, by: null };
    const pts = [];
    for (const fx of [0.1, 0.5, 0.9]) for (const fy of [0.1, 0.5, 0.9]) pts.push([r.left + r.width * fx, r.top + r.height * fy]);
    let covered = 0, total = 0, by = null;
    for (const [x, y] of pts) {
      if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
      total++;
      const top = document.elementFromPoint(x, y);
      if (top && top !== el && !el.contains(top) && !top.contains(el)) {
        const fixed = top.closest('*') && (() => { for (let n = top; n; n = n.parentElement) { const p = getComputedStyle(n).position; if (p === 'fixed' || p === 'sticky') return n; } return null; })();
        if (fixed) { covered++; by = by || describe(fixed); }
      }
    }
    return { covered, total, by };
  }

  function snapshotVisible() {
    const out = new Set();
    for (const el of document.querySelectorAll('body *')) if (visible(el)) out.add(el);
    return out;
  }

  function contentState(limit = 3000) {
    const rows = [];
    let i = 0;
    for (const el of document.querySelectorAll('body *')) {
      if (i++ > limit) break;
      if (!visible(el)) continue;
      const cs = getComputedStyle(el);
      rows.push([el, `${el.className}|${cs.transform}|${cs.left}|${cs.opacity}|${el.children.length ? '' : (el.textContent || '').slice(0, 80)}`]);
    }
    return rows;
  }

  const STOP = {
    en: ['the', 'and', 'of', 'to', 'is', 'in', 'you', 'for', 'with', 'your', 'are', 'this', 'on', 'we', 'our'],
    fr: ['le', 'la', 'les', 'et', 'des', 'est', 'pour', 'vous', 'une', 'dans', 'nous', 'avec', 'sur', 'pas', 'votre'],
    de: ['der', 'die', 'und', 'das', 'ist', 'nicht', 'mit', 'sie', 'ein', 'eine', 'für', 'auf', 'wir', 'ihr', 'zu'],
    es: ['el', 'la', 'de', 'que', 'los', 'las', 'por', 'una', 'para', 'con', 'es', 'su', 'del', 'nuestro', 'usted'],
    it: ['il', 'di', 'che', 'per', 'una', 'sono', 'con', 'non', 'della', 'gli', 'nel', 'questo', 'alla', 'anche', 'noi'],
    nl: ['de', 'het', 'een', 'en', 'van', 'ik', 'je', 'dat', 'niet', 'voor', 'met', 'zijn', 'wij', 'ons', 'uw'],
    pt: ['o', 'que', 'não', 'uma', 'os', 'para', 'com', 'você', 'por', 'mais', 'dos', 'como', 'mas', 'nosso', 'seu'],
  };

  window.__qa = {
    xpath, sig, snippet, describe, byXpath, bySelector, visible, rect,

    describeXpath(xp) { return describe(byXpath(xp)); },
    describeTarget(t) { return describe(bySelector(t)); },
    visuallyHidden, contrastContext,
    contrastXpath(xp) { return contrastContext(byXpath(xp)); },
    contrastTarget(t) { return contrastContext(bySelector(t)); },

    // Links and buttons inside menus that are in the DOM but not displayed (display: none,
    // visibility: hidden, opacity 0 or zero size), grouped by the nearest visible ancestor: the
    // menu item whose :hover may reveal them. run_audit.py hovers each trigger to see which open.
    hiddenMenuItems(limit = 30) {
      const groups = new Map();
      const sel = 'a[href],button,[role=menuitem],[role=link],[role=button]';
      for (const el of document.querySelectorAll(sel)) {
        if (visible(el) || el.closest('[hidden],[inert],template,noscript,dialog:not([open])')) continue;
        if (el.closest('[aria-hidden=true]') && !el.closest('nav,[role=navigation],[role=menubar],[role=menu]')) continue;
        let t = el.parentElement;
        while (t && t !== document.body && !visible(t)) t = t.parentElement;
        if (!t || t === document.body || t === document.documentElement) continue;
        if (!groups.has(t)) groups.set(t, []);
        groups.get(t).push(el);
      }
      const out = [];
      for (const [t, items] of groups) {
        const ctl = [...t.querySelectorAll('a[href],button,[role=button],[role=menuitem],[tabindex]')].find(e => visible(e) && isFocusable(e));
        out.push({ trigger: describe(t), control: ctl ? { ...describe(ctl), tag: ctl.localName,
                   href: ctl.getAttribute('href'), submits: !!(ctl.form && (ctl.type === 'submit' || ctl.type === 'image')), haspopup: ctl.getAttribute('aria-haspopup'), expanded: ctl.getAttribute('aria-expanded') } : null,
                   items: items.slice(0, 20).map(describe) });
        if (out.length >= limit) break;
      }
      return out;
    },
    activeXpath() { const a = deepActive(); return a && a !== document.body ? xpath(a) : null; },

    pageInfo() {
      const vm = document.querySelector('meta[name=viewport]');
      const frames = [...document.querySelectorAll('iframe,frame')].map(f => {
        let same = false; try { same = !!f.contentDocument; } catch (e) { same = false; }
        return { src: f.src || null, same_origin: same, title: f.getAttribute('title'), xpath: xpath(f) };
      });
      const captcha = !!document.querySelector('iframe[src*="recaptcha"],iframe[src*="hcaptcha"],iframe[src*="challenges.cloudflare.com"],.g-recaptcha,.h-captcha,.cf-turnstile');
      return { title: document.title, lang: document.documentElement.getAttribute('lang'), viewport_meta: vm ? vm.content : null,
               template: template(), landmarks: landmarks(), headings: headings(), frames, captcha,
               links: [...document.querySelectorAll('a[href]')].map(a => a.href).filter(h => h.startsWith(location.origin)).slice(0, 500) };
    },

    focusTargets() {
      const all = interactive();
      return { focusable: all.filter(isFocusable).map(describe), pointer_only: pointerOnly().map(describe) };
    },

    activeInfo() {
      const a = deepActive();
      if (!a || a === document.body || a === document.documentElement) return null;
      const d = describe(a);
      d.obscured = obscured(a);
      d.tabindex = a.getAttribute('tabindex');
      d.doc_y = Math.round(a.getBoundingClientRect().top + scrollY);
      d.in_dialog = !!a.closest('dialog,[role=dialog],[aria-modal=true]');
      return d;
    },
    blurActive() { const a = deepActive(); if (a && a.blur) a.blur(); },
    refocus(xp) { const el = byXpath(xp); if (el && el.focus) el.focus({ preventScroll: true }); },
    containerOf(xps) {
      const els = xps.map(byXpath).filter(Boolean);
      if (!els.length) return null;
      let c = els[0].parentElement;
      while (c && !els.every(e => c.contains(e))) c = c.parentElement;
      return describe(c);
    },

    // Pointer targets only: links, buttons, non-text inputs, ARIA widgets and onclick elements. A tabindex
    // alone (a skip-link target such as <div id="main" tabindex="-1">, a scrollable region) is not something
    // to tap; visually hidden targets (.sr-only skip links) aren't seen. Measured at the current scroll
    // position (run_audit.py scrolls to the top first), all in viewport coordinates; a pair where one target
    // is fixed or sticky and the other isn't is never compared, since they only meet at some scroll positions.
    targetSize() {
      const POINTER = 'a[href],area[href],button,input:not([type=hidden]),summary,[onclick],[role=button],[role=link],[role=checkbox],' +
        '[role=radio],[role=switch],[role=tab],[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],[role=option],[role=slider],' +
        '[role=spinbutton],[role=treeitem]';
      const pinned = el => { for (let n = el; n && n.nodeType === 1; n = n.parentElement) { const p = getComputedStyle(n).position; if (p === 'fixed' || p === 'sticky') return true; } return false; };
      const targets = interactive().filter(el => el.matches(POINTER) && !visuallyHidden(el) && !['iframe'].includes(el.localName) && !(el.localName === 'input' && ['text', 'email', 'search', 'tel', 'url', 'password', 'number', 'date'].includes(el.type)) && !['textarea', 'select'].includes(el.localName));
      const boxes = targets.map(el => ({ el, r: el.getBoundingClientRect(), pinned: pinned(el) })).filter(b => b.r.width > 0 && b.r.height > 0);
      const small = boxes.filter(b => b.r.width < 24 || b.r.height < 24);
      const center = r => [r.left + r.width / 2, r.top + r.height / 2];
      const circleHitsRect = (c, r) => { const dx = Math.max(r.left - c[0], 0, c[0] - r.right); const dy = Math.max(r.top - c[1], 0, c[1] - r.bottom); return dx * dx + dy * dy < 144; };
      const out = [];
      for (const b of small) {
        const c = center(b.r);
        const inline = b.el.localName === 'a' && !!b.el.closest('p,li,td,dd') && ((b.el.closest('p,li,td,dd').innerText || '').trim().length > (b.el.innerText || '').trim().length + 20);
        let conflict = null;
        for (const o of boxes) {
          if (o === b || o.el.contains(b.el) || b.el.contains(o.el) || o.pinned !== b.pinned) continue;
          const oc = center(o.r);
          const oSmall = o.r.width < 24 || o.r.height < 24;
          if (circleHitsRect(c, o.r) || (oSmall && Math.hypot(c[0] - oc[0], c[1] - oc[1]) < 24)) { conflict = describe(o.el); break; }
        }
        const d = describe(b.el);
        out.push({ ...d, width: Math.round(b.r.width * 10) / 10, height: Math.round(b.r.height * 10) / 10, inline, spacing_ok: !conflict, conflicts_with: conflict });
      }
      const under44 = boxes.filter(b => b.r.width < 44 || b.r.height < 44).length;
      return { targets: boxes.length, undersized: out, under_44: under44, scroll_y: Math.round(scrollY) };
    },

    reflow() {
      const se = document.scrollingElement;
      const vw = document.documentElement.clientWidth;
      const hscroll = se.scrollWidth > vw + 1;
      const culprits = [], clippedByAnc = [];
      for (const el of document.querySelectorAll('body *')) {
        if (!visible(el) || visuallyHidden(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.right <= vw + 1 && r.left >= -1) continue;
        if (r.right <= 0) continue;  // parked off-screen to the left on purpose (a skip link until focused)
        const p = el.parentElement;
        const pr = p ? p.getBoundingClientRect() : null;
        if (pr && (pr.right > vw + 1 || pr.left < -1) && p !== document.body) continue;  // report the outermost overflowing element
        let scroller = false, clipper = null;
        for (let n = p; n && n !== document.body; n = n.parentElement) {
          const ox = getComputedStyle(n).overflowX;
          if (ox === 'auto' || ox === 'scroll') { scroller = true; break; }
          if (ox === 'hidden' || ox === 'clip') { clipper = n; break; }
        }
        const exempt = el.closest(EXEMPT_REFLOW) ? el.closest(EXEMPT_REFLOW).localName : null;
        if (clipper) {
          // cut off by an ancestor, so it cannot cause page scrolling, but the part past the viewport may be lost
          const cr = clipper.getBoundingClientRect();
          if (cr.right > vw + 1 && clippedByAnc.length < 20)
            clippedByAnc.push({ ...describe(el), exempt_kind: exempt, overflow_px: Math.round(r.right - vw), clipped_by: describe(clipper) });
          continue;
        }
        culprits.push({ ...describe(el), exempt_kind: exempt, inside_scroller: scroller, overflow_px: Math.round(r.right - vw) });
        if (culprits.length >= 20) break;
      }
      return { viewport_width: vw, scroll_width: se.scrollWidth, horizontal_scroll: hscroll, culprits, clipped_by_ancestor: clippedByAnc };
    },

    clipped() {
      const out = [];
      for (const el of document.querySelectorAll('body *')) {
        if (!visible(el)) continue;
        const text = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
        if (!text || visuallyHidden(el)) continue;  // .sr-only text is clipped on purpose and never seen
        const cs = getComputedStyle(el);
        const hideX = ['hidden', 'clip'].includes(cs.overflowX), hideY = ['hidden', 'clip'].includes(cs.overflowY);
        const over = (hideY && el.scrollHeight > el.clientHeight + 1) || (hideX && el.scrollWidth > el.clientWidth + 1) || (cs.textOverflow === 'ellipsis' && el.scrollWidth > el.clientWidth + 1);
        if (over) out.push({ ...describe(el), scroll: [el.scrollWidth, el.scrollHeight], client: [el.clientWidth, el.clientHeight],
                             overflow: Math.max(el.scrollWidth - el.clientWidth, el.scrollHeight - el.clientHeight) });
        if (out.length >= 50) break;
      }
      return out;
    },

    textSpacing(on) {
      const id = '__qa_text_spacing';
      const old = document.getElementById(id);
      if (old) old.remove();
      if (!on) return true;
      const s = document.createElement('style');
      s.id = id;
      s.textContent = '* { line-height: 1.5 !important; letter-spacing: 0.12em !important; word-spacing: 0.16em !important; } p { margin-bottom: 2em !important; } :root { --qa-text-spacing: 1; }';
      (document.head || document.documentElement).appendChild(s);
      // a Content-Security-Policy without 'unsafe-inline' (or with a hash or nonce) refuses the style silently
      return getComputedStyle(document.documentElement).getPropertyValue('--qa-text-spacing').trim() === '1';
    },

    // Where sequential focus navigation starts: a temporary, invisible element at the very top of the body,
    // focused without scrolling, so the next Tab reaches the first focusable element (a skip link) without
    // clicking anything. focusStartDone() removes it.
    focusStart() {
      window.scrollTo(0, 0);
      const a = deepActive(); if (a && a.blur) a.blur();
      let s = document.getElementById('__qa_focus_start');
      if (!s) {
        s = document.createElement('span');
        s.id = '__qa_focus_start';
        s.tabIndex = -1;
        s.style.position = 'absolute'; s.style.top = '0'; s.style.left = '0'; s.style.width = '1px'; s.style.height = '1px';
        s.style.overflow = 'hidden'; s.style.outline = 'none';
        document.body.insertBefore(s, document.body.firstChild);
      }
      s.focus({ preventScroll: true });
      return document.activeElement === s;
    },
    focusStartDone() { const s = document.getElementById('__qa_focus_start'); if (s) s.remove(); },

    visibleSet() { window.__qa_vis = snapshotVisible(); return window.__qa_vis.size; },
    newlyVisible() {
      const before = window.__qa_vis || new Set();
      const added = [...snapshotVisible()].filter(e => !before.has(e));
      const roots = added.filter(e => !added.includes(e.parentElement));
      return roots.slice(0, 5).map(describe);
    },
    hoverCandidates(limit = 40) {
      const sel = '[aria-describedby],[data-tooltip],[data-toggle=tooltip],[aria-haspopup],[aria-expanded],nav li,.tooltip-trigger,.has-tooltip';
      const els = [...document.querySelectorAll(sel)].filter(visible);
      for (const el of interactive()) if (!els.includes(el)) els.push(el);
      return els.slice(0, limit).map(describe);
    },
    stillVisible(xp) { const el = byXpath(xp); return !!el && visible(el); },

    motionSnapshot() { window.__qa_motion = contentState(); return window.__qa_motion.length; },
    motionChanges() {
      const before = window.__qa_motion || [];
      const changed = before.filter(([el, s]) => {
        if (!el.isConnected) return false;
        const cs = getComputedStyle(el);
        return `${el.className}|${cs.transform}|${cs.left}|${cs.opacity}|${el.children.length ? '' : (el.textContent || '').slice(0, 80)}` !== s;
      }).map(([el]) => el);
      const roots = changed.filter(e => !changed.some(o => o !== e && o.contains(e)));
      const MOVING = '[class*=carousel],[class*=slider],[class*=marquee],[class*=ticker],[class*=rotator],[aria-roledescription=carousel]';
      let containers = roots.map(r => r.closest(MOVING) || (r.parentElement && r.parentElement.closest('section,div,ul')) || r);
      containers = [...new Set(containers)].slice(0, 8);
      const anims = document.getAnimations ? document.getAnimations().filter(a => {
        const t = a.effect && a.effect.getTiming ? a.effect.getTiming() : {};
        return a.playState === 'running' && (t.iterations === Infinity || (Number(t.duration) || 0) * (t.iterations || 1) > 5000);
      }).map(a => describe(a.effect && a.effect.target)).filter(Boolean).slice(0, 10) : [];
      const legacy = [...document.querySelectorAll('marquee,blink')].map(describe);
      return {
        changing: containers.map(c => {
          const ctrls = [...c.querySelectorAll('button,[role=button],a[href]'), ...(c.parentElement ? c.parentElement.querySelectorAll(':scope > button,:scope > [role=button]') : [])];
          const pause = ctrls.find(b => /pause|stop|play|halt|freeze/i.test((b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '') + ' ' + (b.title || '')));
          return { ...describe(c), pause_control: pause ? describe(pause) : null };
        }),
        infinite_animations: anims, legacy };
    },

    media() {
      const items = [...document.querySelectorAll('video,audio')].map(m => ({
        ...describe(m), kind: m.localName, autoplay: m.autoplay, muted: m.muted, controls: m.controls, loop: m.loop,
        duration: isFinite(m.duration) ? Math.round(m.duration) : null,
        tracks: [...m.querySelectorAll('track')].map(t => ({ kind: t.kind || 'subtitles', srclang: t.srclang || null, label: t.label || null })) }));
      const embeds = [...document.querySelectorAll('iframe[src*="youtube"],iframe[src*="vimeo"],iframe[src*="wistia"],iframe[src*="player"],object,embed')].map(e => ({ ...describe(e), kind: 'embed', src: e.src || e.data || null }));
      return { items, embeds };
    },

    language() {
      const lang = document.documentElement.getAttribute('lang');
      const valid = lang === null ? null : /^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*$/.test(lang.trim());
      const words = ((document.body && document.body.innerText) || '').toLowerCase().match(/[a-zà-ÿ]+/g) || [];
      const counts = {};
      for (const [k, list] of Object.entries(STOP)) { const set = new Set(list); counts[k] = words.filter(w => set.has(w)).length; }
      const ranked = Object.entries(counts).sort((a, b) => b[1] - a[1]);
      const [top, second] = ranked;
      const detected = top[1] >= 20 && top[1] >= 2 * second[1] ? top[0] : null;
      return { lang, valid, words: words.length, detected, counts };
    },
  };
})();
