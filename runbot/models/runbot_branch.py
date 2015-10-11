# -*- encoding: utf-8 -*-
from openerp import models, fields, api

import re


class RunbotBranch(models.Model):
    _name = "runbot.branch"
    _order = 'name'

    # Fields
    repo_id = fields.Many2one('runbot.repo', string='Repository',
                              required=True, ondelete='cascade', select=1)
    name = fields.Char('Ref Name', required=True)
    branch_name = fields.Char(compute='_get_branch_name', string='Branch',
                              readonly=1, store=True)
    branch_url = fields.Char(compute='_get_branch_url', string='Branch url',
                             readonly=1)
    pull_head_name = fields.Char(compute='_get_pull_head_name',
                                 string='PR HEAD name', readonly=1, store=True)
    sticky = fields.Boolean('Sticky', select=1)
    coverage = fields.Boolean('Coverage')
    state = fields.Char('Status')
    modules = fields.Char("Modules to Install",
                          help="Comma-separated list of modules to install and"
                               " test.")
    job_timeout = fields.Integer('Job Timeout (minutes)',
                                 help='For default timeout: Mark it zero')

    # Methods
    @api.multi
    def _get_pull_info(self):
        self.ensure_one()
        repo = self.repo_id
        if repo.token and self.name.startswith('refs/pull/'):
            pull_number = self.name[len('refs/pull/'):]
            return repo.github('/repos/:owner/:repo/pulls/%s' % pull_number,
                               ignore_errors=True) or {}
        return {}

    @api.depends('name')
    def _get_branch_name(self):
        for rec in self:
            rec.branch_name = rec.name.split('/')[-1]

    @api.depends('name')
    def _get_pull_head_name(self):
        for rec in self:
            pi = rec._get_pull_info()
            if pi:
                rec.pull_head_name = pi['head']['ref']

    @api.depends('branch_name', 'repo_id.base')
    def _get_branch_url(self):
        for rec in self:
            if re.match('^[0-9]+$', rec.branch_name):
                rec.branch_url = "https://%s/pull/%s" % (rec.repo_id.base,
                                                         rec.branch_name)
            else:
                rec.branch_url = "https://%s/tree/%s" % (rec.repo_id.base,
                                                         rec.branch_name)
