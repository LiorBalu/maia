import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'

import he from './he.json'

// Hebrew is the only language for now; the structure allows adding `en` later.
void i18n.use(initReactI18next).init({
  resources: { he: { translation: he } },
  lng: 'he',
  fallbackLng: 'he',
  interpolation: { escapeValue: false },
})

document.documentElement.lang = 'he'
document.documentElement.dir = 'rtl'

export default i18n
