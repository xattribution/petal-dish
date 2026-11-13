import '@fontsource/b612/400.css';
import '@fontsource/b612/700.css';
import '@fontsource/b612-mono/400.css';
import '@fontsource/b612-mono/700.css';
import './styles/tokens.css';
import './styles/app.css';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { loadWorld } from './map/world';
import { boot } from './state/store';
import { initTheme } from './styles/theme';

initTheme();
void boot();
void loadWorld().catch(() => undefined); // borders also give the sites' time zones

const root = document.getElementById('root');
if (root) {
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
