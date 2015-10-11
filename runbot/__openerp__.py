{
    'name': 'Runbot',
    'category': 'Website',
    'summary': 'Runbot',
    'version': '1.3',
    'description': "Runbot",
    'author': 'OpenERP SA',
    'depends': ['website'],
    'external_dependencies': {
        'python': ['matplotlib', 'simplejson'],
    },
    'data': [
        'security/runbot_security.xml',
        'security/ir.model.access.csv',
        'security/ir.rule.csv',
        'views/runbot.xml',
        'views/runbot_repo_view.xml',
        'views/runbot_branch_view.xml',
        'views/runbot_build_view.xml',
        'views/res_config_view.xml',
    ],
    'installable': True,
}
