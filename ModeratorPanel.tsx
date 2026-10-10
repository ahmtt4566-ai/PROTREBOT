export default function ModeratorPanel({onLogout}: {onLogout: () => void}) {
  return <main><h1>Moderatör paneli</h1><p>Yakında</p><a href="/settings">Profil ve güvenlik</a><button onClick={onLogout}>Çıkış</button></main>
}
