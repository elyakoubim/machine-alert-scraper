{
  "name": "W4 — Tally → Clients Airtable + Email bienvenue",
  "flow": [
    {
      "id": 1,
      "module": "gateway:CustomWebHook",
      "version": 1,
      "parameters": {
        "hook": null,
        "maxResults": 1
      },
      "mapper": {},
      "metadata": {
        "designer": {
          "x": 0,
          "y": 0
        }
      }
    },
    {
      "id": 2,
      "module": "builtin:BasicFeeder",
      "version": 1,
      "parameters": {},
      "mapper": {
        "array": "{{1.data.fields}}"
      },
      "metadata": {
        "designer": {
          "x": 300,
          "y": 0
        }
      }
    },
    {
      "id": 3,
      "module": "airtable:ActionCreateRecord",
      "version": 3,
      "parameters": {
        "__IMTCONN__": 6727777
      },
      "mapper": {
        "base": "appQrNOm3Q7D9uEed",
        "useColumnId": false,
        "table": "tblFae1mWP3h1XOy1",
        "fields": {
          "email": "{{1.data.fields[0].value}}",
          "marques": "{{1.data.fields[1].value}}",
          "budget_max": "{{1.data.fields[2].value}}",
          "pays": "{{1.data.fields[3].value}}",
          "types_annonces": "{{1.data.fields[4].value}}",
          "actif": true,
          "date_creation": "{{formatDate(now; 'YYYY-MM-DD')}}",
          "token": "{{replace(uuid; '-'; '')}}"
        }
      },
      "metadata": {
        "designer": {
          "x": 600,
          "y": 0
        },
        "restore": {
          "parameters": {
            "__IMTCONN__": {
              "label": "Airtable - Machine Alert",
              "data": {
                "scoped": "true",
                "connection": "airtable2"
              }
            }
          },
          "expect": {
            "base": {
              "mode": "chose",
              "label": "Machine Alert MVP"
            },
            "table": {
              "mode": "chose",
              "label": "Clients"
            }
          }
        }
      }
    }
  ],
  "metadata": {
    "instant": true,
    "version": 1,
    "scenario": {
      "roundtrips": 1,
      "maxErrors": 3,
      "autoCommit": true,
      "autoCommitTriggerLast": true,
      "sequential": false,
      "confidential": false,
      "dataloss": false,
      "dlq": false,
      "freshVariables": false
    },
    "designer": {
      "orphans": []
    },
    "zone": "eu1.make.com"
  }
}
