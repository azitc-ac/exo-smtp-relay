/* common.js — gemeinsame Frontend-Helfer für Gateway UND Hub.
 *
 * DIESE DATEI MUSS IN BEIDEN ANWENDUNGEN INHALTSGLEICH SEIN.
 * tools/driftcheck.py vergleicht die SHA-256 und schlägt bei Abweichung an.
 * Änderungen also immer in beide Kopien, oder driftcheck meldet es beim
 * nächsten Lauf.
 *
 * Warum es das gibt: es existierten elf handgeschriebene HTML-Escaper mit elf
 * verschiedenen Namen (_esc, escHtml, _escH, _escT, _escAttr, escC, escR,
 * escP, esc …). Zwei davon waren binnen einer Sitzung ReferenceErrors, weil der
 * Name an der Aufrufstelle nicht zum Namen an der Definition passte. Ein
 * gemeinsamer Name kann nicht falsch geschrieben werden, ohne sofort aufzufallen.
 */

/* HTML-Text maskieren. Für alles, was per innerHTML/Template-String in die Seite
 * kommt und nicht aus dem eigenen Code stammt: Server-Antworten, Fehlertexte
 * fremder Dienste, Namen, E-Mail-Adressen, Dateinamen. */
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

/* Für Werte, die in ein Attribut gehen (value="…", title="…").
 * Identisch zu esc(), aber als eigener Name, damit an der Aufrufstelle
 * erkennbar bleibt, in welchem Zusammenhang maskiert wird. Zeilenumbrüche
 * werden zusätzlich entfernt — in einem Attribut haben sie nichts zu suchen
 * und brechen die Darstellung. */
function escAttr(s) {
  return esc(s).replace(/[\r\n]+/g, ' ');
}

/* Kurzmeldung an einem Element: Text + Zustand + einblenden.
 * Ersetzt die diversen _autoMsg/_showMsg-Varianten.
 *
 * Der Zustand geht als data-state hinaus und NIE als style.color/background
 * (CLAUDE.md Regel 2): Der Browser normalisiert JS-gesetzte Inline-Styles zu
 * rgb(), und die Dark-Mode-Attribut-Selektoren [style*="…#hex"] greifen dann
 * nicht mehr. Die Farbe gehört ins CSS, das beide Modi abdeckt. */
function showMsg(el, text, ok) {
  if (typeof el === 'string') el = document.getElementById(el);
  if (!el) return;
  el.textContent = text;
  el.dataset.state = ok ? 'ok' : 'err';
  el.style.display = 'block';
}

/* JSON-POST mit einheitlicher Fehlerbehandlung. Wirft nicht, sondern liefert
 * immer ein Objekt mit ok/error — damit Aufrufstellen nicht jeweils eigene
 * try/catch-Varianten bauen. */
async function postJSON(url, body) {
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    const ct = r.headers.get('content-type') || '';
    const d = ct.indexOf('application/json') === 0 ? await r.json() : {};
    if (!r.ok && !d.message && !d.error && !d.detail) {
      return { ok: false, error: 'HTTP ' + r.status };
    }
    if (d.ok === undefined) d.ok = r.ok;
    return d;
  } catch (e) {
    return { ok: false, error: 'Netzwerkfehler: ' + e };
  }
}

/* GET mit einheitlicher Fehlerbehandlung. Liefert immer ein Objekt:
 * bei Netzwerk- oder HTTP-Fehler { ok:false, error:"…" } statt zu werfen.
 * Für Anzeige-Widgets, die sonst still leer blieben. */
async function getJSON(url) {
  try {
    const r = await fetch(url);
    const ct = r.headers.get('content-type') || '';
    const d = ct.indexOf('application/json') === 0 ? await r.json() : {};
    if (!r.ok) return { ok: false, error: d.error || d.message || d.detail || ('HTTP ' + r.status) };
    if (d.ok === undefined) d.ok = true;
    return d;
  } catch (e) {
    return { ok: false, error: 'Netzwerkfehler: ' + e };
  }
}

/* Wie postJSON, aber mit frei wählbarer Methode — für DELETE und PUT. */
async function sendJSON(method, url, body) {
  try {
    const opts = { method: method };
    if (body !== undefined && body !== null) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }
    const r = await fetch(url, opts);
    const ct = r.headers.get('content-type') || '';
    const d = ct.indexOf('application/json') === 0 ? await r.json() : {};
    if (!r.ok && !d.message && !d.error && !d.detail) return { ok: false, error: 'HTTP ' + r.status };
    if (d.ok === undefined) d.ok = r.ok;
    return d;
  } catch (e) {
    return { ok: false, error: 'Netzwerkfehler: ' + e };
  }
}

/* Fehlertext aus einer Antwort von getJSON/postJSON/sendJSON. */
function errText(d) {
  return (d && (d.error || d.message || d.detail)) || 'Unbekannter Fehler';
}

/* Betrag in Cent als Euro-Text. War in mehreren Vorlagen einzeln nachgebaut. */
function eur(cents) {
  return (Number(cents || 0) / 100).toLocaleString('de-DE', {
    style: 'currency', currency: 'EUR',
  });
}

/* ── Markdown ────────────────────────────────────────────────────────────────
 * Kleiner Wandler für die Texte, die als Markdown vorliegen: Rechtstexte,
 * CA-Bedingungen, Changelog-Einträge. Lag als `_mdToHtml` lokal in
 * settings_connect.html — mit der Folge, dass die Changelog-Anzeige in der
 * Update-Sektion den Text ROH ausgab: sichtbare `**`, Backticks und
 * Tabellenstriche. Ein zweiter Wandler wäre die falsche Antwort gewesen.
 *
 * Bewusst klein gehalten: Überschriften, Listen, Tabellen, Trennlinien, fett,
 * kursiv, Code, Verweise. Kein vollständiges Markdown — was hier ankommt,
 * schreiben wir selbst.
 *
 * Farben stammen aus der freigegebenen Palette (CLAUDE.md); ohne Angabe griffe
 * beim Verweis das Browser-Standardblau, das im Dark Mode schlecht lesbar ist.
 */
function _mdInline(s) {
  return esc(s)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    // Kursiv NACH fett — danach sind keine ** mehr übrig
    .replace(/\*([^*\n]+)\*/g,'<em>$1</em>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    // color:#0369a1 stammt aus der freigegebenen Palette (CLAUDE.md) und wird im
    // Dark Mode zu #7dd3fc umgeschaltet. Ohne Angabe griffe das Browser-Standardblau,
    // das auf dunklem Grund schlecht lesbar ist — eine globale a-Regel gibt es nicht.
    .replace(/(https?:\/\/[^\s<)]+)/g,
             '<a href="$1" target="_blank" rel="noopener" style="color:#0369a1">$1</a>');
}
function _mdToHtml(md) {
  var out = [], tbl = null;
  // In Absätze zerlegen (Leerzeile trennt), Tabellen/Listen bleiben zeilenweise
  var blocks = String(md || '').replace(/\r\n/g, '\n').split(/\n{2,}/);
  blocks.forEach(function(block) {
    var lines = block.split('\n').filter(function(l) { return l.trim() !== ''; });
    if (!lines.length) return;
    // Tabelle: mindestens zwei Zeilen, alle beginnen mit |
    if (lines.length >= 2 && lines.every(function(l) { return l.trim().charAt(0) === '|'; })) {
      var rows = lines.filter(function(l) { return !/^\|[\s:|-]+\|$/.test(l.trim()); });
      var html = '<table style="width:100%;border-collapse:collapse;margin:8px 0;font-size:12px">';
      rows.forEach(function(l, i) {
        var cells = l.trim().replace(/^\||\|$/g, '').split('|');
        var tag = i === 0 ? 'th' : 'td';
        html += '<tr>' + cells.map(function(c) {
          return '<' + tag + ' style="border:1px solid #e2e8f0;padding:4px 8px;text-align:left">'
                 + _mdInline(c.trim()) + '</' + tag + '>';
        }).join('') + '</tr>';
      });
      out.push(html + '</table>');
      return;
    }
    // Überschrift
    var h = lines[0].match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      var lvl = Math.min(h[1].length + 1, 4);
      out.push('<h' + lvl + ' style="margin:14px 0 6px;font-size:' + (16 - h[1].length) + 'px">'
               + _mdInline(h[2]) + '</h' + lvl + '>');
      lines = lines.slice(1);
      if (!lines.length) return;
    }
    // Trennlinie
    if (lines.every(function(l) { return /^-{3,}$/.test(l.trim()); })) {
      out.push('<hr style="border:none;border-top:1px solid #e2e8f0;margin:12px 0">');
      return;
    }
    // Liste
    if (lines[0].trim().charAt(0) === '-') {
      var items = [], cur = '';
      lines.forEach(function(l) {
        if (/^\s*-\s+/.test(l)) { if (cur) items.push(cur); cur = l.replace(/^\s*-\s+/, ''); }
        else { cur += ' ' + l.trim(); }     // Fortsetzungszeile anhängen
      });
      if (cur) items.push(cur);
      out.push('<ul style="margin:6px 0 6px 18px;padding:0">'
        + items.map(function(i) { return '<li style="margin:2px 0">' + _mdInline(i) + '</li>'; }).join('')
        + '</ul>');
      return;
    }
    // Normaler Absatz: harte Umbrüche zu Leerzeichen zusammenziehen
    out.push('<p style="margin:0 0 10px">' + _mdInline(lines.join(' ')) + '</p>');
  });
  return out.join('');
}

/* Ursache eines gefangenen Fehlers sichtbar machen.
 *
 * Anlass (2026-07-27): `_reflowPlain()` rief `_mdEscape()` auf — eine Funktion,
 * die es nicht gab. Der ReferenceError landete in einem catch-Zweig, der ihn
 * verwarf und stattdessen „Nutzungsbedingungen konnten nicht geladen werden"
 * anzeigte. Die Meldung zeigte damit auf den Hub, der einwandfrei antwortete.
 * Hätte dort „… : _mdEscape is not defined" gestanden, wäre die Ursache sofort
 * sichtbar gewesen statt nach Wochen.
 *
 * Deshalb tut die Funktion beides: sie protokolliert den vollständigen Fehler
 * samt Aufrufkette in der Konsole UND liefert einen kurzen Anhang für die
 * Anzeige. Rückgabe ist ein FERTIGES Textstück inklusive Klammern, damit die
 * Aufrufstelle nur anhängen muss:
 *
 *     catch (e) { el.textContent = 'Netzwerkfehler' + ursache(e, 'certTerms'); }
 *
 * Ist keine Ursache zu ermitteln, bleibt es beim bisherigen Satz — ein
 * angehängtes „(undefined)" hilft niemandem.
 */
function ursache(e, ort) {
  try { console.error('[' + (ort || 'unbekannt') + ']', e); } catch (_) { /* Konsole fehlt */ }
  var t = '';
  if (e == null) t = '';
  else if (typeof e === 'string') t = e;
  else t = e.message || e.name || String(e);
  t = String(t).trim();
  if (!t || t === '[object Object]') return '';
  return ' (' + (t.length > 160 ? t.slice(0, 157) + '…' : t) + ')';
}


/* Fehler DIREKT am betroffenen Feld anzeigen — nicht am Ende des Abschnitts.
 *
 * Anlass (2026-07-29): „Mindestbetrag 25 €." erschien in der Sammelmeldung ganz
 * unten, während das Eingabefeld weiter oben stand. Bei einem langen Abschnitt
 * sieht man beides nie gleichzeitig; man liest eine Rüge und muss suchen,
 * worauf sie sich bezieht.
 *
 * Die Meldung wird als Geschwister direkt hinter das Feld gehängt und beim
 * nächsten Aufruf wiederverwendet, damit sich bei mehrfacher Eingabe nichts
 * stapelt. `fieldClear()` räumt sie weg, sobald der Wert wieder stimmt.
 *
 * Farbe kommt aus dem CSS (.field-msg[data-state]) — hier wird nur der Zustand
 * gesetzt, nie eine Farbe (siehe CLAUDE.md, Dark-Mode-Regel 2).
 */
function fieldMsg(el, text, ok) {
  if (typeof el === 'string') el = document.getElementById(el);
  if (!el) return;
  var box = el.nextElementSibling;
  if (!box || !box.classList || !box.classList.contains('field-msg')) {
    box = document.createElement('div');
    box.className = 'field-msg';
    el.parentNode.insertBefore(box, el.nextSibling);
  }
  box.textContent = text;
  box.dataset.state = ok ? 'ok' : 'err';
  box.style.display = 'block';
  // Auch das Feld selbst kennzeichnen: bei mehreren Eingaben nebeneinander ist
  // sonst nicht zu sehen, welches gemeint ist.
  el.dataset.invalid = ok ? '' : '1';
  // Liegt das Feld in einem eingeklappten Bereich, wäre die Rüge unsichtbar —
  // der Vorgang bräche scheinbar grundlos ab. Deshalb alle umschliessenden
  // <details> aufklappen. Das gehört hierher und nicht an die Aufrufstellen,
  // sonst muss jede künftige daran denken.
  if (!ok && el.closest) {
    var d = el.closest('details');
    while (d) {
      d.open = true;
      d = d.parentElement && d.parentElement.closest ? d.parentElement.closest('details') : null;
    }
  }
}

function fieldClear(el) {
  if (typeof el === 'string') el = document.getElementById(el);
  if (!el) return;
  var box = el.nextElementSibling;
  if (box && box.classList && box.classList.contains('field-msg')) box.style.display = 'none';
  el.dataset.invalid = '';
}


/* Lange Erklärtexte auf zwei Zeilen kürzen, mit „mehr"/„weniger".
 *
 * Anlass (2026-07-29): 38 Hinweistexte, mehrere davon 300–480 Zeichen. Sie
 * drängen das Bedienbare nach unten und werden gerade deshalb nicht gelesen.
 *
 * Warum die Zeilenbegrenzung per CSS und kein Aufteilen am ersten Satz: Text zu
 * zerlegen geht an Abkürzungen („z.B.", „i.S.d.", „Ziffer 6.11") schief. Die
 * Begrenzung braucht den Inhalt gar nicht zu kennen, wirkt bei jedem künftigen
 * Text und lässt sich ohne Änderung an den Vorlagen einführen.
 *
 * Ohne JavaScript bleibt der volle Text stehen — die Kürzung ist eine Zutat,
 * keine Voraussetzung fürs Lesen.
 *
 * Nur Block-Elemente. Die Kürzung setzt `display:-webkit-box`; bei einem
 * inline stehenden Hinweis hinter einem Feld macht das aus ihm einen Block und
 * verschiebt das Layout, ohne Platz zu sparen.
 *
 * ⚠️ Bis 2026-08-19 war das als „keine span" umgesetzt — eine Näherung, die
 * daneben lag: Auf den Einstellungsseiten stehen Hinweise regelmässig als
 * `span.hint` mit `display:block` unter dem Feld. Zwei davon liefen auf einem
 * Telefon über vier Zeilen, ohne je einen Schalter zu bekommen. Entscheidend
 * ist nicht das Tag, sondern wie das Element tatsächlich dargestellt wird —
 * und das steht nicht im Quelltext, sondern erst im Browser.
 *
 * ENTSCHEIDEND: gemessen, nicht geschätzt. Die erste Fassung hängte den
 * Schalter an jeden Text ab 150 Zeichen. Wie viel davon sichtbar ist, hängt
 * aber an der Breite des Kastens: In einer breiten Karte stehen 250 Zeichen
 * bequem in zwei Zeilen — der Schalter versprach dann „mehr", und beim Klick
 * kam nichts dazu. Ein Bedienelement, das nichts tut, ist schlimmer als keins.
 *
 * Also wird nach dem Setzen der Begrenzung nachgesehen, ob der Text
 * tatsächlich überläuft (scrollHeight > clientHeight). Nur dann bekommt er
 * einen Schalter.
 */
function _hintMessbar(p) {
  // Unsichtbar (eingeklappter Bereich, geschlossenes <details>, Karte mit
  // display:none): Höhen sind 0, jede Messung wertlos.
  return !!p.offsetParent || p.offsetHeight > 0;
}

function _hintBewerten(p, schalter) {
  var vorher = p.dataset.clamp;
  p.dataset.clamp = 'zu';
  // Nicht "ein Pixel mehr", sondern "mindestens eine weitere Zeile".
  // Im Browser gemessen: drei Absätze liefen um genau 2px über — Rundung und
  // Unterlängen, kein verborgener Inhalt. Ihr Schalter erschien, klappte auf
  // und zeigte exakt dasselbe. Bei -webkit-line-clamp:2 ist clientHeight/2
  // eine Zeile; ein Viertel davon liegt sicher über jedem Rundungsrest und
  // sicher unter einer echten Zeile.
  var laeuftUeber = p.scrollHeight - p.clientHeight > p.clientHeight / 4;
  if (!laeuftUeber) {
    p.dataset.clamp = 'aus';                                // CSS greift nur bei "zu"
  } else if (vorher === 'auf') {
    p.dataset.clamp = 'auf';                                // vom Nutzer geöffnet: so lassen
  }
  if (schalter) schalter.style.display = laeuftUeber ? '' : 'none';
  return laeuftUeber;
}

function _hintAusstatten(p) {
  if (!_hintBewerten(p, null)) return;
  var schalter = document.createElement('button');
  schalter.type = 'button';
  schalter.className = 'hint-toggle';
  schalter.textContent = 'mehr';
  schalter.addEventListener('click', function () {
    var zu = p.dataset.clamp === 'zu';
    p.dataset.clamp = zu ? 'auf' : 'zu';
    schalter.textContent = zu ? 'weniger' : 'mehr';
  });
  p.parentNode.insertBefore(schalter, p.nextSibling);
}

/* Noch unsichtbare Texte: Bewertung aufschieben, bis sie eine Box haben.
 *
 * Vorher entschied hier die Textlänge. Auf der Anbindungsseite, die ihre
 * Abschnitte nachlädt, erzeugte das acht Schalter, hinter denen nichts steckte
 * — gemessen im Browser. Genau der Fall, den die Messung eigentlich
 * abschaffen sollte, nur an anderer Stelle. */
var _hintBeobachter = null;
function _hintAufschieben(p) {
  if (typeof IntersectionObserver === 'undefined') {
    // Ohne Beobachter bleibt nur die Schätzung — betrifft nur sehr alte Browser
    if ((p.textContent || '').trim().length >= 150) _hintAusstatten(p);
    return;
  }
  p.dataset.clampWait = '1';   // damit ein zweiter Lauf ihn nicht erneut aufnimmt
  if (!_hintBeobachter) {
    _hintBeobachter = new IntersectionObserver(function (eintraege) {
      eintraege.forEach(function (e) {
        if (!e.isIntersecting) return;
        _hintBeobachter.unobserve(e.target);
        delete e.target.dataset.clampWait;
        _hintAusstatten(e.target);
      });
    });
  }
  _hintBeobachter.observe(p);
}

function initHintClamps(root) {
  var scope = root || document;
  var texte = scope.querySelectorAll(
    '.hint:not([data-clamp]):not([data-clamp-wait])');
  Array.prototype.forEach.call(texte, function (p) {
    if (!_hintMessbar(p)) { _hintAufschieben(p); return; }
    // Inline dargestellte Hinweise bleiben aussen vor — siehe Kopfkommentar.
    var anzeige = getComputedStyle(p).display;
    if (anzeige === 'inline' || anzeige === 'inline-block') return;
    _hintAusstatten(p);
  });
}


/* Nachgerenderte Abschnitte: neue Erklärtexte selbst aufnehmen.
 *
 * `initHintClamps()` erfasst, was beim Aufruf im DOM steht. Ein Hinweis, den
 * eine Ladefunktion später einfügt, bekommt nie einen Schalter — auch der
 * IntersectionObserver hilft nicht, denn angemeldet wird nur, was der Lauf
 * gesehen hat.
 *
 * ⚠️ Ehrlichkeitshalber: Am 19.08.2026 vermutet, genau das sei der Grund für
 * vier ungekürzte Texte auf der Anbindungsseite. Nachgemessen war es das NICHT
 * — die vier warteten korrekt auf den IntersectionObserver (`data-clamp-wait`)
 * und bekamen ihren Schalter, sobald man hinscrollte. Ein messbarer Fall für
 * diesen Beobachter existiert derzeit nicht: Alle nachgerenderten Hinweise
 * sind kürzer als die Schwelle.
 *
 * Er bleibt trotzdem, weil die Lücke echt ist und nur zufällig leer — der
 * nächste lange nachgerenderte Text fiele sonst durch, und man suchte wieder
 * von vorn. Entprellt, weil beim Nachladen viele Änderungen kurz hintereinander
 * kommen.
 */
var _hintNachzuegler;
if (typeof MutationObserver !== 'undefined') {
  new MutationObserver(function () {
    clearTimeout(_hintNachzuegler);
    _hintNachzuegler = setTimeout(function () { initHintClamps(document); }, 120);
  }).observe(document.documentElement, {childList: true, subtree: true});
}

/* Bei geänderter Fensterbreite passt derselbe Text plötzlich in zwei Zeilen
 * — oder eben nicht mehr. Ohne Neubewertung bliebe ein Schalter stehen, der
 * nichts mehr aufzuklappen hat. Vom Nutzer geöffnete Texte bleiben offen. */
var _hintZeitgeber;
window.addEventListener('resize', function () {
  clearTimeout(_hintZeitgeber);
  _hintZeitgeber = setTimeout(function () {
    document.querySelectorAll('.hint[data-clamp]').forEach(function (p) {
      if (p.dataset.clamp === 'auf') return;
      var s = p.nextElementSibling;
      _hintBewerten(p, s && s.classList.contains('hint-toggle') ? s : null);
    });
  }, 150);
});


// ── Zuletzt getroffene Auswahl merken ────────────────────────────────────────
//
// Wer immer dieselbe Signatur prüft, soll sie nicht bei jedem Öffnen neu
// wählen müssen. Gleichzeitig darf eine gemerkte Wahl, die es nicht mehr gibt
// (Postfach entfernt, Vorlage gelöscht), nie zu einer leeren Auswahl führen —
// dann stünde die Seite mit gefülltem Feld und leerer Fläche da.
//
// Bewusst allgemein: dieselbe Regel gilt für Postfach, Signatur, Banner und
// Disclaimer. Vier Kopien derselben drei Zeilen wären genau die Streuung, die
// hier schon einmal auseinanderlief.
// Der LEERE Wert wird mitgemerkt, nicht gelöscht: „— keine —" ist eine
// Entscheidung und keine fehlende Angabe. Wer den Banner bewusst weglässt,
// soll ihn beim nächsten Öffnen nicht wieder vorgesetzt bekommen.
//
// Für Auswahlen, in denen der leere Wert gar nicht vorkommt (das Postfach),
// bleibt es folgenlos: `auswahlWaehlen()` verwirft ihn, weil er nicht in der
// Liste der erlaubten Werte steht, und greift zur Vorgabe.
function auswahlMerken(schluessel, wert) {
  try { localStorage.setItem(schluessel, wert || ''); }
  catch (e) { /* privates Fenster o.ä. — dann eben ohne Gedächtnis */ }
}

function auswahlLesen(schluessel) {
  try { return localStorage.getItem(schluessel); }
  catch (e) { return null; }
}

// `erlaubte` sind die tatsächlich vorhandenen Werte. `vorgabe` greift, wenn
// nichts gemerkt ist oder das Gemerkte verschwunden ist; ohne Vorgabe fällt es
// auf den ersten Eintrag zurück.
function auswahlWaehlen(sel, schluessel, erlaubte, vorgabe) {
  const gemerkt = auswahlLesen(schluessel);
  let wahl;
  if (gemerkt !== null && erlaubte.includes(gemerkt)) wahl = gemerkt;
  else if (vorgabe !== undefined && erlaubte.includes(vorgabe)) wahl = vorgabe;
  else wahl = erlaubte[0] || '';
  sel.value = wahl;
  return wahl;
}

// Das Vorschau-Postfach — gemeinsam für Editor-Live-Vorschau und
// Vorschau-Seite. Zwei Kopien liefen sonst auseinander, und wer zwischen
// beiden wechselt, bekäme unterschiedliche Vorauswahlen.
const VORSCHAU_POSTFACH_SCHLUESSEL = 'exo.vorschau.postfach';

function vorschauPostfachMerken(email) {
  auswahlMerken(VORSCHAU_POSTFACH_SCHLUESSEL, email);
}

function vorschauPostfachWaehlen(sel, adressen) {
  // Ohne Vorgabe: das erste Postfach der Liste. Eine leere Auswahl wäre hier
  // nutzlos — es gibt nichts anzuzeigen, solange kein Postfach gewählt ist.
  const wahl = auswahlWaehlen(sel, VORSCHAU_POSTFACH_SCHLUESSEL, adressen);
  return wahl;
}


/* ────────────────────────────────────────────────────────────────────────────
 * Ungespeicherte Änderungen sichtbar machen
 *
 * ANLASS (19.08.2026): Auf den Einstellungsseiten stehen 44 Speichern-Knöpfe.
 * Für sich genommen ist jeder erklärbar, zusammen ergeben sie keine Linie: Vor
 * einem Feld ist nicht zu erkennen, ob es einen Knopf braucht, welchen, und ob
 * das Drücken etwas bewirkt hat. Einzelne Schalter speichern sofort, andere
 * nicht — optisch identisch.
 *
 * `speicherWache()` beantwortet beides am selben Ort:
 *
 *   unverändert  → Knopf ist ausgegraut, daneben steht nichts
 *   geändert     → Knopf wird bedienbar, daneben „noch nicht gespeichert"
 *   gespeichert  → „gespeichert" für ein paar Sekunden, dann wieder still
 *
 * Der Knopf ist damit kein stummes Angebot mehr, sondern eine Aussage: Solange
 * er grau ist, gibt es nichts zu sichern.
 *
 * ⚠️ Farben ausschliesslich über `data-zustand` im CSS (siehe style.css und
 * dark-mode.css). In JS gesetzte Farben normalisiert der Browser zu rgb(), und
 * die Dark-Mode-Selektoren greifen dann nicht mehr — das ist in diesem Projekt
 * mehrfach passiert und steht als verbindliche Regel in CLAUDE.md.
 * ──────────────────────────────────────────────────────────────────────────── */

/* Vergleichswert über alle Felder eines Abschnitts.
 *
 * JSON statt Aneinanderhängen: Bei `join('')` wären ["ab", "c"] und ["a", "bc"]
 * derselbe Wert — zwei Felder, deren Inhalte zusammen gleich bleiben, während
 * sich beide geändert haben. Selten, aber lautlos.
 */
function _speicherStand(els) {
  return JSON.stringify(els.map(_speicherWert));
}

function _speicherWert(el) {
  if (!el) return '';
  if (el.type === 'checkbox' || el.type === 'radio') return el.checked ? '1' : '0';
  return el.value != null ? String(el.value) : '';
}

// ── Container-Modus für speicherWache ───────────────────────────────────────
//
// Vier Speichern-Knöpfe liessen sich nicht überwachen, weil ihre Felder nicht
// feststehen: Benutzer-Overrides, eigene Variablen und S/MIME-Regeln bestehen
// aus Zeilen, die zur Laufzeit entstehen und verschwinden; die Key-Vault-Wahl
// ist eine Radiogruppe ohne id.
//
// `data-wache-container="#id"` überwacht stattdessen alles, was in einem
// Bereich steht — auch das, was es beim Laden noch nicht gab.
//
// ⚠️ Die ANZAHL der Zeilen gehört in den Vergleichswert. Wer eine leere Zeile
// hinzufügt oder eine gefüllte löscht, hat etwas geändert; ohne die Anzahl
// wäre „drei leere Felder" derselbe Stand wie „keine Felder".

function _speicherFelder(container) {
  return Array.prototype.slice.call(
    container.querySelectorAll('input, select, textarea'));
}

function _speicherStandContainer(container) {
  const els = _speicherFelder(container);
  return JSON.stringify([els.length].concat(els.map(_speicherWert)));
}

/* knopf   — der Speichern-Knopf (Element oder id)
 * felder  — Elemente oder ids, deren Änderung diesen Knopf betrifft
 * options — {hinweisId} für ein vorhandenes Meldungsfeld; sonst wird eines
 *           direkt hinter den Knopf gesetzt.
 *
 * Rückgabe: {erledigt(), fehlgeschlagen(), zuruecksetzen()} — `erledigt()`
 * nach erfolgreichem Speichern aufrufen; es merkt sich den neuen Stand als
 * „unverändert".
 */
function speicherWache(knopf, felder, options) {
  const opt = options || {};
  const btn = typeof knopf === 'string' ? document.getElementById(knopf) : knopf;
  const behaelter = opt.container
    ? (typeof opt.container === 'string' ? document.querySelector(opt.container) : opt.container)
    : null;
  // ⚠️ Bereich UND Einzelfelder, nicht Bereich ODER Einzelfelder.
  //
  // Bis 2026-08-25 verdrängte ein gesetzter Bereich die Feldliste. Das reichte,
  // solange alles Überwachte beieinanderstand — beim Signatur-Baukasten steht
  // es das nicht: Die Bausteine liegen in der linken Spalte, der Betreff einer
  // Nachricht an Postfachinhaber darüber im Kopf. Den Bereich weiter zu fassen
  // wäre der falsche Ausweg gewesen; dann läge die Postfachauswahl der Vorschau
  // mit darin und jeder Wechsel dort meldete eine ungespeicherte Änderung.
  const els = (felder || [])
    .map(f => (typeof f === 'string' ? document.getElementById(f) : f))
    .filter(Boolean);
  if (!btn || (!behaelter && !els.length)) {
    return {erledigt() {}, fehlgeschlagen() {}, zuruecksetzen() {}};
  }
  const standJetzt = () => JSON.stringify([
    behaelter ? _speicherStandContainer(behaelter) : 0,
    els.length ? _speicherStand(els) : 0,
  ]);

  let hinweis = opt.hinweisId ? document.getElementById(opt.hinweisId) : null;
  if (!hinweis) {
    hinweis = document.createElement('span');
    btn.insertAdjacentElement('afterend', hinweis);
  }
  hinweis.classList.add('speicher-hinweis');

  let stand = standJetzt();
  let timer = null;

  // ⚠️ DER BEOBACHTER DARF SICH NICHT SELBST AUSLÖSEN.
  //
  // ANLASS (2026-08-26), Nutzer: „Alle Seiten antworten nur Signaturen nicht.
  // Ich sehe sie wenn sie fertig geladen ist, aber dann ist Essig."
  //
  // `zeichnen()` ändert selbst das DOM — `btn.disabled` und den Hinweistext.
  // Liegt der Knopf INNERHALB des beobachteten Bereichs, meldet der
  // MutationObserver genau diese Änderung, ruft `zeichnen()` erneut, und das
  // wieder eine Mutation: eine Endlosschleife, die den Hauptthread festhält.
  // Die Seite erscheint noch fertig gerendert und nimmt danach keinen Klick
  // mehr an — auch das Hauptmenü nicht, denn das hängt am selben Thread.
  //
  // Beim Signatur-Baukasten war das der Fall (Knopf in `#baukasten-felder`),
  // bei den älteren Container-Wachen zufällig nicht — dort steht der Knopf
  // ausserhalb der Tabelle. Auf diesen Zufall darf sich nichts verlassen.
  //
  // `beobachter.disconnect()` vor der Änderung und `observe()` danach:
  // Mutationen, die aus dem Zeichnen selbst stammen, kommen damit gar nicht
  // erst in die Warteschlange.
  let beobachter = null;
  const BEOBACHTET = {childList: true, subtree: true,
                      attributes: true, characterData: true};

  function zeichnen() {
    if (beobachter) beobachter.disconnect();
    try {
      const jetzt = standJetzt();
      const offen = jetzt !== stand;
      btn.disabled = !offen;
      _speicherLeisteZeichnen();
      if (offen) {
        clearTimeout(timer);
        hinweis.dataset.zustand = 'offen';
        hinweis.textContent = '← noch nicht gespeichert';
      } else if (hinweis.dataset.zustand === 'offen') {
        hinweis.dataset.zustand = '';
        hinweis.textContent = '';
      }
    } finally {
      // Auch nach einem Fehler wieder anhängen — sonst hört die Wache still auf.
      if (beobachter && behaelter) beobachter.observe(behaelter, BEOBACHTET);
    }
  }

  if (behaelter) {
    // Delegiert statt je Feld gebunden: Zeilen entstehen und verschwinden zur
    // Laufzeit, und eine Bindung an ein Feld, das es noch nicht gibt, geht ins
    // Leere. Der Beobachter fängt zusätzlich das Hinzufügen und Löschen selbst.
    behaelter.addEventListener('input', zeichnen);
    behaelter.addEventListener('change', zeichnen);
    if (typeof MutationObserver !== 'undefined') {
      beobachter = new MutationObserver(zeichnen);
      beobachter.observe(behaelter, BEOBACHTET);
    }
  } else {
    els.forEach(el => {
      el.addEventListener('input', zeichnen);
      el.addEventListener('change', zeichnen);
    });
  }
  zeichnen();
  _speicherKnoepfe.push(btn);

  return {
    erledigt(text) {
      stand = standJetzt();
      btn.disabled = true;
      _speicherLeisteZeichnen();
      hinweis.dataset.zustand = 'fertig';
      hinweis.textContent = text || '✓ gespeichert';
      clearTimeout(timer);
      timer = setTimeout(() => {
        // Nur löschen, wenn inzwischen nichts Neues geändert wurde.
        if (hinweis.dataset.zustand === 'fertig') {
          hinweis.dataset.zustand = '';
          hinweis.textContent = '';
        }
      }, 4000);
    },
    fehlgeschlagen(text) {
      hinweis.dataset.zustand = 'fehler';
      hinweis.textContent = text || '✗ nicht gespeichert';
      btn.disabled = false;
      _speicherLeisteZeichnen();
    },
    zuruecksetzen() {
      stand = standJetzt();
      zeichnen();
    },
  };
}


/* Mitlaufende Leiste: der Speichern-Knopf kommt zum Benutzer.
 *
 * ANLASS (2026-08-19): Gemessen auf einem Telefon (393×850) liegen zwischen
 * einem geänderten Feld und seinem Knopf bis zu **zwei Bildschirmhöhen** —
 * Benachrichtigungen 1740 px, S/MIME 1313 px. Wer oben etwas ändert, scrollt
 * an ein bis zwei fremden Speichern-Knöpfen vorbei (die richtigerweise
 * gesperrt sind) und muss den eigenen erst finden.
 *
 * Die Leiste erscheint nur, wenn tatsächlich etwas offen ist, und verschwindet
 * nach dem Sichern. Sie ersetzt den Knopf am Abschnitt nicht — dort steht er
 * weiterhin, damit die Zuordnung sichtbar bleibt; sie erspart nur den Weg.
 *
 * Bei mehreren offenen Abschnitten wird nicht stillschweigend alles gesichert:
 * Die Leiste sagt, wie viele es sind, und speichert sie der Reihe nach erst auf
 * Klick. Ein „Speichern", das ungefragt fremde Abschnitte mitnimmt, wäre
 * dieselbe Überraschung wie eine unangekündigte Abbuchung.
 */
function _speicherLeiste() {
  var el = document.getElementById('speicher-leiste');
  if (el) return el;
  el = document.createElement('div');
  el.id = 'speicher-leiste';
  el.className = 'speicher-leiste';
  el.hidden = true;
  el.innerHTML = '<span class="speicher-leiste-text"></span>' +
                 '<button type="button" class="btn primary btn-sm">Speichern</button>';
  el.querySelector('button').addEventListener('click', function () {
    // Der Reihe nach: Jeder Knopf meldet sein Ergebnis selbst über seine Wache.
    _speicherKnoepfe.filter(function (b) { return b && !b.disabled; })
                    .forEach(function (b) { b.click(); });
  });
  document.body.appendChild(el);
  return el;
}

/* Beschriftung des Abschnitts, zu dem ein Wächter-Knopf gehört.
 *
 * ANLASS: Ein Passwort-Autofill löste „ungespeicherte Änderung"
 * aus, aber die Leiste sagte nur „Eine Änderung …" — bei maskierten Feldern in
 * einem eingeklappten Abschnitt fand der Nutzer nicht, WAS sich geändert hatte.
 * Der Name (bevorzugt `data-wache-label`, sonst die Überschrift des Abschnitts)
 * macht die Leiste zur Sprungmarke. Ohne auffindbaren Namen bleibt der bisherige
 * Text — kein Rückschritt. */
function _wacheLabel(btn) {
  if (btn.dataset && btn.dataset.wacheLabel) return btn.dataset.wacheLabel.trim();
  var sec = btn.closest && btn.closest('.settings-card, .card, .wizard-step, section, fieldset');
  if (sec) {
    var h = sec.querySelector('h1, h2, h3, h4, legend, .card-title, .step-title');
    if (h && h.textContent.trim()) return h.textContent.trim();
  }
  return '';
}

function _speicherLeisteZeichnen() {
  var offen = _speicherKnoepfe.filter(function (b) { return b && !b.disabled; });
  var el = _speicherLeiste();
  if (!offen.length) { el.hidden = true; return; }
  el.hidden = false;
  var namen = [];
  offen.forEach(function (b) {
    var l = _wacheLabel(b);
    if (l && namen.indexOf(l) === -1) namen.push(l);
  });
  var text;
  if (offen.length === 1) {
    text = namen.length ? 'Noch nicht gespeichert: ' + namen[0]
                        : 'Eine Änderung ist noch nicht gespeichert';
  } else {
    text = offen.length + ' Abschnitte noch nicht gespeichert'
         + (namen.length ? ': ' + namen.slice(0, 3).join(', ')
                            + (namen.length > 3 ? ' …' : '') : '');
  }
  var span = el.querySelector('.speicher-leiste-text');
  span.textContent = text;
  // Sprungmarke: Klick auf den Text führt zum ersten offenen Abschnitt.
  span.style.cursor = 'pointer';
  span.onclick = function () {
    offen[0].scrollIntoView({behavior: 'smooth', block: 'center'});
  };
}

/* Kein Zeitgeber: `speicherWache()` ruft das nach jedem Zustandswechsel auf.
 * Ein Intervall, das viermal je Sekunde nachsieht, ob sich etwas geändert hat,
 * ist Arbeit für den Fall, dass nichts passiert. */

/* Alle überwachten Knöpfe der Seite — Grundlage für die Warnung beim Verlassen. */
const _speicherKnoepfe = [];

/* Ein bedienbarer Speichern-Knopf heisst: es liegt etwas Ungesichertes an.
 * Der Browser fragt dann beim Verlassen nach — die letzte Sicherung gegen
 * „ich dachte, das sei übernommen". */
window.addEventListener('beforeunload', (e) => {
  if (_speicherKnoepfe.some(b => b && !b.disabled)) {
    e.preventDefault();
    e.returnValue = '';
  }
});


/* Deklarative Einrichtung: `<button id="x" data-wache="feld1,feld2">`
 *
 * Siebzehn Knöpfe von Hand zu verdrahten (id suchen, Felderliste pflegen,
 * beim Laden registrieren, Ergebnis zurückmelden) ist vier Gelegenheiten je
 * Knopf, etwas zu vergessen — und eine vergessene Wache sieht aus wie eine,
 * die nichts zu melden hat. Deshalb steht die Zuordnung dort, wo sie hingehört:
 * am Knopf.
 *
 * Speichernde Funktionen melden das Ergebnis mit `wacheFertig(id, ok)`.
 */
const _wachen = new Map();

function wacheEinrichten(wurzel) {
  // ⚠️ BEIDE Attribute. `data-wache` nennt feste Felder, `data-wache-container`
  // einen Bereich mit wechselndem Inhalt. Wer nur das erste sucht, richtet für
  // Container-Knöpfe gar keine Wache ein — und ein Knopf ohne Wache sieht aus
  // wie einer, bei dem gerade nichts zu tun ist.
  (wurzel || document)
    .querySelectorAll('button[data-wache], button[data-wache-container]')
    .forEach(btn => {
    if (!btn.id || _wachen.has(btn.id)) return;
    const felder = (btn.dataset.wache || '').split(',').map(s => s.trim()).filter(Boolean);
    const behaelter = btn.dataset.wacheContainer || '';
    if (!felder.length && !behaelter) return;
    _wachen.set(btn.id, speicherWache(btn, felder,
                                      {hinweisId: btn.dataset.wacheHinweis || null,
                                       container: behaelter || null}));
  });
}

/* Den Ausgangsstand NEU messen — ohne etwas zu melden.
 *
 * ⚠️ Nötig, sobald der überwachte Bereich ASYNCHRON gefüllt wird. Die Wache
 * misst ihren Ausgangsstand bei `DOMContentLoaded`; kommt der Inhalt erst
 * danach per `fetch`, vergleicht sie hinterher gegen einen leeren Bereich.
 * Zwei Folgen, beide falsch: Auf der frisch geöffneten Seite steht „noch nicht
 * gespeichert", und ein Bereich, der leer bleibt (kein Konto, Ladefehler),
 * lässt den Knopf dauerhaft gesperrt — er sieht dann aus wie einer, bei dem es
 * nichts zu tun gibt.
 *
 * Am Ende der Ladefunktion aufrufen, nicht nach jeder Änderung: Der Sinn ist
 * „so sah es aus, als es ankam". */
function wacheNeuMessen(knopfId) {
  const w = _wachen.get(knopfId);
  if (w) w.zuruecksetzen();
}

/* ok=true → „gespeichert“, ok=false → „nicht gespeichert“, Knopf bleibt bedienbar. */
function wacheFertig(knopfId, ok, text) {
  const w = _wachen.get(knopfId);
  if (!w) return;
  if (ok === false) w.fehlgeschlagen(text); else w.erledigt(text);
}

document.addEventListener('DOMContentLoaded', () => wacheEinrichten(document));


/* Einstellungen schreiben — die EINE Fassung.
 *
 * Stand 19.08.2026 gab es diese Funktion viermal, in vier Vorlagen, bis auf
 * den Fehlerkontext wortgleich. Drei davon lieferten keinen Rückgabewert,
 * weshalb der Aufrufer nicht wissen konnte, ob es geklappt hat — eine
 * Erfolgsmeldung erschien auch nach HTTP 400.
 *
 * Farben kommen aus `.speicher-hinweis[data-zustand]` (style.css), nicht aus
 * `el.style.color`: JS-gesetzte Farben normalisiert der Browser zu rgb(), und
 * der Dark-Mode-Selektor greift dann nicht mehr.
 *
 * Rückgabe: true bei Erfolg, false sonst.
 */
async function savePartial(payload, resultElId, ort) {
  const el = resultElId ? document.getElementById(resultElId) : null;
  const melde = (zustand, text) => {
    if (!el) return;
    el.classList.add('speicher-hinweis');
    el.dataset.zustand = zustand;
    el.textContent = text;
    if (zustand === 'fertig') {
      setTimeout(() => {
        if (el.dataset.zustand === 'fertig') { el.dataset.zustand = ''; el.textContent = ''; }
      }, 4000);
    }
  };
  try {
    const resp = await fetch('/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    melde(resp.ok ? 'fertig' : 'fehler', resp.ok ? '✓ gespeichert' : '✗ nicht gespeichert');
    return resp.ok;
  } catch (e) {
    melde('fehler', '✗ Netzwerkfehler' + ursache(e, ort || 'savePartial'));
    return false;
  }
}


/* Zustimmung zu den Bedingungen einer Zertifizierungsstelle einholen.
 *
 * Stand 20.08.2026 gab es diesen Dialog nur auf der S/MIME-Seite, für die
 * Bestellung eines einzelnen Zertifikats. Der Sammellauf und die automatische
 * Bestellung kannten ihn nicht und gaben deshalb gar keinen Beleg mit — im
 * Livelauf scheiterten vier von vier Bestellungen daran, jede einzeln.
 *
 * `betreff` ist das, wofür zugestimmt wird: eine Adresse oder „4 Postfächer".
 * Liefert true/false; der Zeitstempel entsteht beim Aufrufer, denn nur dort ist
 * bekannt, WANN zugestimmt wurde.
 */
function caBedingungenZeigen(betreff, caLabel, termsUrl, docs, knopfText) {
  return new Promise(function(resolve) {
    var overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9000;display:flex;align-items:center;justify-content:center';
    overlay.innerHTML = `
      <div style="background:#fff;border-radius:10px;padding:24px 28px;max-width:480px;width:90%;box-shadow:0 8px 32px rgba(0,0,0,.18)">
        <h3 style="margin:0 0 12px;font-size:16px">Bedingungen der Zertifizierungsstelle</h3>
        <p style="font-size:13px;color:#374151;margin:0 0 14px">
          Für <strong>${esc(betreff)}</strong> wird ein S/MIME-Zertifikat über
          <strong>${esc(caLabel)}</strong> beantragt. Als Zertifikatnehmer müssen Sie
          den Bedingungen der Zertifizierungsstelle zustimmen.
        </p>
        <p style="margin:0 0 10px">
          <a href="${escAttr(termsUrl)}" target="_blank" rel="noopener"
             style="font-size:13px;color:#2563eb;text-decoration:underline">
            ↗ Bedingungen der Zertifizierungsstelle lesen (neues Tab)
          </a>
        </p>
        ${(docs || []).length ? `<p style="margin:0 0 16px;font-size:12px;color:#6b7280">
          Zum Nachschlagen — nicht Gegenstand der Zustimmung:<br>
          ${(docs || []).map(function(d) {
            return `<a href="${escAttr(d.url)}" target="_blank" rel="noopener"
                       style="color:#6b7280;text-decoration:underline">${esc(d.titel || d.url)}</a>`;
          }).join(' · ')}
        </p>` : ''}
        <label style="display:flex;align-items:flex-start;gap:8px;font-size:13px;cursor:pointer;margin-bottom:18px">
          <input type="checkbox" id="_ca-terms-cb" style="margin-top:2px;flex-shrink:0">
          <span>Ich habe die Bedingungen der Zertifizierungsstelle gelesen und akzeptiere sie.</span>
        </label>
        <div style="display:flex;justify-content:flex-end;gap:10px">
          <button id="_ca-terms-cancel" class="btn secondary" style="font-size:13px">Abbrechen</button>
          <button id="_ca-terms-ok" class="btn primary" style="font-size:13px" disabled>${esc(knopfText || 'Bestellung aufgeben')}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    var cb = overlay.querySelector('#_ca-terms-cb');
    var okBtn = overlay.querySelector('#_ca-terms-ok');
    cb.addEventListener('change', function() { okBtn.disabled = !cb.checked; });
    overlay.querySelector('#_ca-terms-cancel').addEventListener('click', function() {
      document.body.removeChild(overlay); resolve(false);
    });
    okBtn.addEventListener('click', function() {
      document.body.removeChild(overlay); resolve(true);
    });
  });
}

/* ── Erweiterte Einstellungen ein-/ausblenden ─────────────────────────────────
 *
 * Bis 2026-08-26 stand dieses Paar WORTGLEICH in vier Vorlagen (settings,
 * settings_signature, settings_smime, settings_connect). Vier Kopien einer
 * Funktion sind vier Stellen, an denen eine Änderung vergessen werden kann —
 * dieselbe Klasse Befund wie bei den elf handgeschriebenen HTML-Maskierern.
 *
 * `id` benennt den Bereich (`sig`, `smime`, …). Alles mit `data-adv="<id>"`
 * folgt dem Schalter; die Wahl überlebt einen Seitenwechsel im
 * Sitzungsspeicher des Browsers.
 */
function _initAdv(id) {
  var show = localStorage.getItem('advsec_' + id) === '1';
  var cb = document.getElementById('adv-cb-' + id);
  if (cb) cb.checked = show;
  document.querySelectorAll('[data-adv="' + id + '"]').forEach(function(el) {
    el.style.display = show ? '' : 'none';
  });
}

function _toggleAdv(id, show) {
  localStorage.setItem('advsec_' + id, show ? '1' : '0');
  document.querySelectorAll('[data-adv="' + id + '"]').forEach(function(el) {
    el.style.display = show ? '' : 'none';
  });
}
