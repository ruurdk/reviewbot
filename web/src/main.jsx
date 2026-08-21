import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { ThemeProvider } from "styled-components";
import { themes } from "@redis-ui/styles";
import "@redis-ui/styles/fonts.css";
import "@redis-ui/styles/normalized-styles.css";
import { App } from "./App";

/**
 * Theme wiring notes, learned from reading the package rather than guessing:
 *
 * - `themes` is a ThemePair: { light, dark } theme objects for
 *   styled-components' ThemeProvider.
 * - redis-ui also ships `SwitchableModeThemeProvider`, but it owns the mode via
 *   localStorage and does not expose the *resolved* mode to consumers (it
 *   destructures appThemeMode away). We need the resolved mode to pick chart
 *   colours, so we hold that state ourselves and hand the theme object down.
 * - Dark mode is a selected theme, never an inverted one: the chart ramps have
 *   their own validated dark steps (src/theme/series.js).
 */
function Root() {
  const [mode, setMode] = useState("light");
  const theme = mode === "dark" ? themes.dark : themes.light;
  return (
    <ThemeProvider theme={theme}>
      <div style={{ background: theme?.semantic?.color?.background?.neutral100 ?? "transparent", minHeight: "100vh" }}>
        <App mode={mode} onToggleMode={() => setMode((m) => (m === "dark" ? "light" : "dark"))} />
      </div>
    </ThemeProvider>
  );
}

createRoot(document.getElementById("root")).render(<Root />);
