import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import './index.css'
import App from './App.jsx'
import { loadRuntimeConfig } from './api/runtimeConfig'

// Resolve the current tunnel URLs from R2 before first render (never throws;
// falls back to the VITE_* env vars). .finally so a failed/timed-out fetch
// still renders the app.
loadRuntimeConfig().finally(() => {
  createRoot(document.getElementById('root')).render(
    <StrictMode>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </StrictMode>,
  )
})
