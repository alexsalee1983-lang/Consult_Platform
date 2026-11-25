import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";

// 四期 质量：ESLint 官方推荐规则 + React Hooks 两条经典规则；配色只用 theme.css 令牌（JS/JSX 里不写 #hex / rgb() 颜色）
const COLOR = "/#[0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]|rgba?[(]/";
const NO_COLOR = "颜色只用 theme.css 令牌（var(--xx)），不写硬编码颜色";
export default [
  { ignores: ["dist/", "test-results/", "src/survey/sw.template.js"] },   // sw.template.js 是构建时填占位符的模板
  js.configs.recommended,
  {
    files: ["**/*.{js,jsx,mjs}"],
    plugins: { "react-hooks": reactHooks },
    languageOptions: {
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: { ...globals.browser, ...globals.node },
    },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "no-irregular-whitespace": ["error", { skipJSXText: true }],   // 界面文案里的全角空格是有意的
      "no-restricted-syntax": ["error",
        { selector: `Literal[value=${COLOR}]`, message: NO_COLOR },
        { selector: `TemplateElement[value.raw=${COLOR}]`, message: NO_COLOR }],
    },
  },
];
