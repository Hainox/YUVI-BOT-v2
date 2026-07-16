// Mini App is a pure client-rendered SPA (adapter-static, fallback: '200.html').
// SSR/prerendering are meaningless here — every screen depends on
// window.Telegram.WebApp (initData, start_param) which only exists in the
// browser, and the app is always served behind the fallback page anyway.
export const ssr = false;
export const prerender = false;
