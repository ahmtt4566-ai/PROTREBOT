import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import {assetInlineLimit} from '../vite-asset-policy'
export default defineConfig({plugins:[react()],resolve:{dedupe:['react','react-dom']},build:{assetsInlineLimit:assetInlineLimit},server:{port:5173,strictPort:true,proxy:{'^/api(?:/|$)':{target:'http://127.0.0.1:8000',changeOrigin:true}}}})
