// ESLint para los .html del panel Nexa (JS embebido en <script>).
// Analiza el JavaScript DENTRO del HTML — que es lo que el LSP no puede hacer.
//   Uso:  npx eslint *.html
// Requiere: eslint 9 + eslint-plugin-html + globals  (npx los baja solo).
const html = require("eslint-plugin-html");
const globals = require("globals");

module.exports = [
  {
    files: ["**/*.html"],
    plugins: { html },
    languageOptions: {
      ecmaVersion: 2021,
      sourceType: "script",
      globals: {
        ...globals.browser,
        // Librerías cargadas por <script src> (CDN) que viven en otro archivo:
        supabase: "readonly",
        XLSX: "readonly",
        Chart: "readonly",
        Papa: "readonly",
        L: "readonly",
        // Refs legacy protegidas por `typeof ... !== 'undefined'` (no rompen, revisar algún día):
        cortesPorCuad: "readonly",
        waNum: "readonly",
      },
    },
    rules: {
      // Lo que de verdad importa: usar algo que no existe (función mal escrita, global implícita).
      "no-undef": "error",
      // Ruido en código sin tipos — apagado a propósito:
      "no-unused-vars": "off",
      "no-redeclare": "off",
      "no-cond-assign": "off",
      "no-empty": "off",
      "no-control-regex": "off",
    },
  },
];
