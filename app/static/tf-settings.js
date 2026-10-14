/* Master setup, client side: one store for the instance-wide region, time zone,
   date/time format and language, the formatters every date and time on the
   dashboard goes through, and the translation lookup for the navigation and the
   Master setup screen.

   Loaded before the dashboard scripts. Everything it exposes is on `window`
   with a `tf` prefix, and nothing else leaks into the shared script scope. Values
   are stored on the server (GET/PUT /api/settings); until that answers, the
   defaults below apply, and they match the server's defaults so the first paint
   is already correct. */
(function () {
  const DEFAULTS = Object.freeze({
    region: "GLOBAL", timezone: "UTC", date_format: "YYYY-MM-DD", time_format: "24h",
    language: "en", default_windows_version: "windows_11", screen_zoom: "fit"
  });
  let current = DEFAULTS;
  let catalog = null;
  const listeners = new Set();

  // Returns the same object until something really changes: useSyncExternalStore
  // compares snapshots with Object.is, so a fresh object per call would loop.
  function tfSettings() { return current; }
  window.tfSettings = tfSettings;
  window.tfSettingsCatalog = () => catalog;
  window.tfSubscribeSettings = (listener) => { listeners.add(listener); return () => listeners.delete(listener); };
  window.tfSetSettings = (settings, options) => {
    if (options) catalog = options;
    if (!settings || typeof settings !== "object") return;
    const next = { ...DEFAULTS, ...settings };
    if (Object.keys(DEFAULTS).every(key => next[key] === current[key])) return;
    current = Object.freeze(next);
    try { document.documentElement.lang = current.language; } catch (_) { /* no document */ }
    listeners.forEach(listener => listener());
  };

  // Dates from the API are UTC. A timestamp without a zone is treated as UTC too,
  // because the server never writes a local time.
  const toDate = (value) => {
    if (value === null || value === undefined || value === "") return null;
    let date;
    if (value instanceof Date) {
      date = value;
    } else if (typeof value === "number") {
      // Epoch seconds from the import APIs, or milliseconds from the browser.
      date = new Date(value < 1e11 ? value * 1000 : value);
    } else {
      let text = String(value);
      if (/T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(text)) text += "Z";
      date = new Date(text);
    }
    return Number.isNaN(date.getTime()) ? null : date;
  };

  const zoneOf = (settings) => {
    try { new Intl.DateTimeFormat("en-US", { timeZone: settings.timezone }); return settings.timezone; }
    catch (_) { return "UTC"; }
  };

  const clock = (date, zone) => {
    const map = {};
    new Intl.DateTimeFormat("en-US", {
      timeZone: zone, year: "numeric", month: "numeric", day: "numeric",
      hour: "numeric", minute: "numeric", second: "numeric", hourCycle: "h23"
    }).formatToParts(date).forEach(part => { map[part.type] = part.value; });
    return { y: Number(map.year), m: Number(map.month), d: Number(map.day),
             H: Number(map.hour), M: Number(map.minute), S: Number(map.second) };
  };

  const pad = (n) => String(n).padStart(2, "0");
  const languageTag = (settings) => settings.language || "en";

  function formatDate(value, settings = current) {
    const date = toDate(value);
    if (!date) return "";
    const zone = zoneOf(settings);
    const p = clock(date, zone);
    switch (settings.date_format) {
      case "DD/MM/YYYY": return `${pad(p.d)}/${pad(p.m)}/${p.y}`;
      case "MM/DD/YYYY": return `${pad(p.m)}/${pad(p.d)}/${p.y}`;
      case "DD.MM.YYYY": return `${pad(p.d)}.${pad(p.m)}.${p.y}`;
      case "DD-MM-YYYY": return `${pad(p.d)}-${pad(p.m)}-${p.y}`;
      case "YYYY/MM/DD": return `${p.y}/${pad(p.m)}/${pad(p.d)}`;
      case "D MMM YYYY": {
        const month = new Intl.DateTimeFormat(languageTag(settings), { timeZone: zone, month: "short" }).format(date);
        return `${p.d} ${month} ${p.y}`;
      }
      default: return `${p.y}-${pad(p.m)}-${pad(p.d)}`;
    }
  }

  function formatTime(value, settings = current) {
    const date = toDate(value);
    if (!date) return "";
    const p = clock(date, zoneOf(settings));
    if (settings.time_format === "12h") {
      const hour = p.H % 12 || 12;
      return `${hour}:${pad(p.M)}:${pad(p.S)} ${p.H < 12 ? "AM" : "PM"}`;
    }
    return `${pad(p.H)}:${pad(p.M)}:${pad(p.S)}`;
  }

  // The zone is named by its IANA id: "Europe/Berlin" is unambiguous where an
  // abbreviation such as "CET" or "GMT+2" is not.
  const zoneName = (settings) => zoneOf(settings);

  function formatDateTime(value, settings = current) {
    const date = formatDate(value, settings);
    return date ? `${date} ${formatTime(value, settings)}` : "";
  }

  // Date, time and the zone they are shown in: what a run or step timestamp uses.
  function formatStamp(value, settings = current) {
    const text = formatDateTime(value, settings);
    return text ? `${text} · ${zoneName(settings)}` : "";
  }

  // Sample text for the Master setup preview and the format pickers.
  window.tfFormatDate = formatDate;
  window.tfFormatTime = formatTime;
  window.tfFormatDateTime = formatDateTime;
  window.tfFormatStamp = formatStamp;
  window.tfZoneName = zoneName;

  // ---- translation -------------------------------------------------------------
  // Keys are the English text. A missing translation shows English, so adding a
  // language only needs the strings it covers. Navigation and the Master setup
  // screen are translated; the rest of the dashboard is still English.
  const STRINGS = {
    de: {
      "Demo": "Demo", "Projects": "Projekte", "Variables": "Variablen", "AI Recording": "KI-Aufnahme",
      "Windows Desktop": "Windows-Desktop", "Test Plan": "Testplan", "Test Library": "Testbibliothek",
      "Runs": "Läufe", "Compare": "Vergleichen", "Artifacts": "Artefakte", "Logs": "Protokolle",
      "GitHub": "GitHub", "Azure": "Azure", "Future release planning": "Planung künftiger Versionen",
      "Settings": "Einstellungen",
      "Master setup": "Grundeinstellungen",
      "One place for the region, time zone, date and time format and language used across the dashboard. Saved on this server, so every user sees the same values.":
        "Eine Stelle für Region, Zeitzone, Datums- und Zeitformat sowie Sprache im gesamten Dashboard. Auf diesem Server gespeichert, daher sehen alle Benutzer dieselben Werte.",
      "Region": "Region",
      "Choosing a region fills in the defaults below. Each one can still be changed on its own.":
        "Eine Region setzt die Standardwerte unten. Jeder Wert kann danach einzeln geändert werden.",
      "Time zone": "Zeitzone", "Date format": "Datumsformat", "Time format": "Zeitformat",
      "24-hour": "24-Stunden", "12-hour": "12-Stunden", "Language": "Sprache",
      "Search time zones": "Zeitzonen suchen",
      "Preview": "Vorschau", "Shown with the current settings, before you save.": "Mit den aktuellen Einstellungen, vor dem Speichern.",
      "Application defaults": "Anwendungsstandards",
      "Windows version preselected for a new Windows desktop recording": "Vorausgewählte Windows-Version für eine neue Windows-Desktop-Aufnahme",
      "Default screen zoom for the live recording and run screens": "Standard-Zoom für die Live-Bildschirme von Aufnahme und Lauf",
      "SAVE SETTINGS": "EINSTELLUNGEN SPEICHERN", "UNDO CHANGES": "ÄNDERUNGEN VERWERFEN",
      "Saving…": "Speichern…", "Loading settings…": "Einstellungen werden geladen…",
      "Unsaved changes": "Nicht gespeicherte Änderungen",
      "Saved. Dates, times and language across the dashboard now follow these settings.":
        "Gespeichert. Datum, Uhrzeit und Sprache im Dashboard folgen jetzt diesen Einstellungen.",
      "Translated so far: the navigation and this screen. Other text stays in English until it is translated.":
        "Bisher übersetzt: die Navigation und dieser Bildschirm. Anderer Text bleibt auf Englisch, bis er übersetzt ist.",
      "Fit the whole screen": "Ganzen Bildschirm einpassen", "Fit": "Einpassen", "Enlarge": "Vergrößern", "Close": "Schließen",
      "Live Windows screen": "Live-Windows-Bildschirm", "Screen zoom": "Bildschirm-Zoom"
    },
    fr: {
      "Demo": "Démo", "Projects": "Projets", "Variables": "Variables", "AI Recording": "Enregistrement IA",
      "Windows Desktop": "Bureau Windows", "Test Plan": "Plan de test", "Test Library": "Bibliothèque de tests",
      "Runs": "Exécutions", "Compare": "Comparer", "Artifacts": "Artefacts", "Logs": "Journaux",
      "GitHub": "GitHub", "Azure": "Azure", "Future release planning": "Planification des versions futures",
      "Settings": "Paramètres",
      "Master setup": "Configuration générale",
      "One place for the region, time zone, date and time format and language used across the dashboard. Saved on this server, so every user sees the same values.":
        "Un seul endroit pour la région, le fuseau horaire, le format de date et d'heure et la langue utilisés dans tout le tableau de bord. Enregistré sur ce serveur, donc tous les utilisateurs voient les mêmes valeurs.",
      "Region": "Région",
      "Choosing a region fills in the defaults below. Each one can still be changed on its own.":
        "Choisir une région renseigne les valeurs par défaut ci-dessous. Chacune peut encore être modifiée séparément.",
      "Time zone": "Fuseau horaire", "Date format": "Format de date", "Time format": "Format d'heure",
      "24-hour": "24 heures", "12-hour": "12 heures", "Language": "Langue",
      "Search time zones": "Rechercher un fuseau horaire",
      "Preview": "Aperçu", "Shown with the current settings, before you save.": "Affiché avec les réglages actuels, avant l'enregistrement.",
      "Application defaults": "Valeurs par défaut de l'application",
      "Windows version preselected for a new Windows desktop recording": "Version de Windows présélectionnée pour un nouvel enregistrement de bureau Windows",
      "Default screen zoom for the live recording and run screens": "Zoom par défaut des écrans en direct de l'enregistrement et de l'exécution",
      "SAVE SETTINGS": "ENREGISTRER", "UNDO CHANGES": "ANNULER LES MODIFICATIONS",
      "Saving…": "Enregistrement…", "Loading settings…": "Chargement des paramètres…",
      "Unsaved changes": "Modifications non enregistrées",
      "Saved. Dates, times and language across the dashboard now follow these settings.":
        "Enregistré. Les dates, heures et la langue du tableau de bord suivent maintenant ces paramètres.",
      "Translated so far: the navigation and this screen. Other text stays in English until it is translated.":
        "Traduit jusqu'ici : la navigation et cet écran. Le reste du texte reste en anglais jusqu'à sa traduction.",
      "Fit the whole screen": "Adapter tout l'écran", "Fit": "Adapter", "Enlarge": "Agrandir", "Close": "Fermer",
      "Live Windows screen": "Écran Windows en direct", "Screen zoom": "Zoom de l'écran"
    },
    es: {
      "Demo": "Demo", "Projects": "Proyectos", "Variables": "Variables", "AI Recording": "Grabación IA",
      "Windows Desktop": "Escritorio Windows", "Test Plan": "Plan de pruebas", "Test Library": "Biblioteca de pruebas",
      "Runs": "Ejecuciones", "Compare": "Comparar", "Artifacts": "Artefactos", "Logs": "Registros",
      "GitHub": "GitHub", "Azure": "Azure", "Future release planning": "Planificación de versiones futuras",
      "Settings": "Ajustes",
      "Master setup": "Configuración general",
      "One place for the region, time zone, date and time format and language used across the dashboard. Saved on this server, so every user sees the same values.":
        "Un solo lugar para la región, la zona horaria, el formato de fecha y hora y el idioma de todo el panel. Se guarda en este servidor, así que todos los usuarios ven los mismos valores.",
      "Region": "Región",
      "Choosing a region fills in the defaults below. Each one can still be changed on its own.":
        "Elegir una región rellena los valores predeterminados de abajo. Cada uno se puede cambiar por separado.",
      "Time zone": "Zona horaria", "Date format": "Formato de fecha", "Time format": "Formato de hora",
      "24-hour": "24 horas", "12-hour": "12 horas", "Language": "Idioma",
      "Search time zones": "Buscar zonas horarias",
      "Preview": "Vista previa", "Shown with the current settings, before you save.": "Se muestra con la configuración actual, antes de guardar.",
      "Application defaults": "Valores predeterminados de la aplicación",
      "Windows version preselected for a new Windows desktop recording": "Versión de Windows preseleccionada para una nueva grabación de escritorio Windows",
      "Default screen zoom for the live recording and run screens": "Zoom predeterminado de las pantallas en vivo de grabación y ejecución",
      "SAVE SETTINGS": "GUARDAR AJUSTES", "UNDO CHANGES": "DESHACER CAMBIOS",
      "Saving…": "Guardando…", "Loading settings…": "Cargando ajustes…",
      "Unsaved changes": "Cambios sin guardar",
      "Saved. Dates, times and language across the dashboard now follow these settings.":
        "Guardado. Las fechas, las horas y el idioma del panel ahora siguen estos ajustes.",
      "Translated so far: the navigation and this screen. Other text stays in English until it is translated.":
        "Traducido hasta ahora: la navegación y esta pantalla. El resto del texto sigue en inglés hasta que se traduzca.",
      "Fit the whole screen": "Ajustar toda la pantalla", "Fit": "Ajustar", "Enlarge": "Ampliar", "Close": "Cerrar",
      "Live Windows screen": "Pantalla Windows en vivo", "Screen zoom": "Zoom de pantalla"
    }
  };

  window.tfT = (text) => {
    const table = STRINGS[current.language];
    return (table && table[text]) || text;
  };
})();
