# -*- coding: utf-8 -*-
##############################################################################
#
#    OpenERP, Open Source Business Applications
#    Copyright (C) 2004-2012 OpenERP S.A. (<http://openerp.com>).
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

from openerp import models, fields, api


class RunbotConfigSettings(models.TransientModel):
    _name = 'runbot.config.settings'
    _inherit = 'res.config.settings'

    default_workers = fields.Integer('Total Number of Workers')
    default_running_max = fields.Integer('Maximum Number of Running Builds')
    default_timeout = fields.Integer('Default Timeout (in seconds)')
    default_starting_port = fields.Integer('Starting Port for Running Builds')
    default_domain = fields.Char('Runbot Domain')

    @api.model
    def get_default_parameters(self, fields):
        ConfigParameter = self.env['ir.config_parameter']
        workers = ConfigParameter.get_param('runbot.workers', default=6)
        running_max = ConfigParameter.get_param('runbot.running_max',
                                                default=75)
        timeout = ConfigParameter.get_param('runbot.timeout', default=1800)
        starting_port = ConfigParameter.get_param('runbot.starting_port',
                                                  default=2000)
        runbot_domain = ConfigParameter.get_param('runbot.domain',
                                                  default='runbot.odoo.com')
        return {
            'default_workers': int(workers),
            'default_running_max': int(running_max),
            'default_timeout': int(timeout),
            'default_starting_port': int(starting_port),
            'default_domain': runbot_domain,
        }

    @api.multi
    def set_default_parameters(self):
        self.ensure_one()
        ConfigParameter = self.env['ir.config_parameter']
        ConfigParameter.set_param('runbot.workers', self.default_workers)
        ConfigParameter.set_param('runbot.running_max',
                                  self.default_running_max)
        ConfigParameter.set_param('runbot.timeout', self.default_timeout)
        ConfigParameter.set_param('runbot.starting_port',
                                  self.default_starting_port)
        ConfigParameter.set_param('runbot.domain', self.default_domain)
