import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
export default defineConfig({
	plugins:[react()],
	publicDir:'frontend/public',
	define:{'import.meta.env.VITE_BUILD_COMMIT':JSON.stringify(process.env.VITE_BUILD_COMMIT || 'unknown')},
	server:{port:5173,strictPort:true},
})
