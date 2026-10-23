import urdfUrl from '../../../urdf/fairino5_v6.urdf?url'

// Imports serve local assets in dev and emit hashed files in production.
const meshes = import.meta.glob('../../../urdf/fairino5_v6/*.STL', {
  eager: true, query: '?url', import: 'default',
})
export const robotUrdfUrl = urdfUrl
export const robotMeshUrls = Object.fromEntries(
  Object.entries(meshes).map(([path, url]) => [path.split('/').pop(), url]),
)
export function resolveRobotAsset(url) {
  return robotMeshUrls[url.split('/').pop()] || url
}
