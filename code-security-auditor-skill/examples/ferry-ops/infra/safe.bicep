// Safe counterparts: nothing here should match a built-in pattern.
param location string = resourceGroup().location
@secure()
param dbAdminPassword string
param opsGroupId string

resource db 'Microsoft.DBforPostgreSQL/flexibleServers@2023-06-01-preview' = {
  name: 'ferry-db-private'
  location: location
  properties: {
    administratorLogin: 'ferryadmin'
    administratorLoginPassword: dbAdminPassword
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
    }
    network: {
      publicNetworkAccess: 'Disabled'
    }
  }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'ferry-kv-rbac'
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
  }
}

resource site 'Microsoft.Web/sites@2023-01-01' = {
  name: 'ferry-ops-web'
  location: location
  properties: {
    httpsOnly: true
    siteConfig: {
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
    }
  }
}
