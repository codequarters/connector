# -*- encoding: utf-8 -*-
##############################################################################
#
#    Author Guewen Baconnier. Copyright Camptocamp SA
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################
import time
import pooler
import logging
from osv import osv, fields
from tools.translate import _
from tools.safe_eval import safe_eval
from tools import DEFAULT_SERVER_DATETIME_FORMAT
import simplejson


class external_report(osv.osv):
    _name = 'external.report'
    _description = 'External Report'
    _order = 'end_date desc'

    _columns = {
        'name': fields.char('Action', size=32, required=True,
                            readonly=True),
        'object_name': fields.char('Ressource Name', size=64, required=True,
                            readonly=True),
        'object_related': fields.char('Report Related To', size=64, required=True,
                            readonly=True),
        'object_related_description': fields.char('Report Related To', size=64, required=True,
                            readonly=True),
        'res_id': fields.integer('Ressource id', required=True, readonly=True), 
        'method': fields.char('Method', size=64, required=True,
                           readonly=True,
                           help="Method linked to the report"),
        'start_date': fields.datetime('Last Start Date', readonly=True),
        'end_date': fields.datetime('Last End Date', readonly=True),
        'referential_id': fields.many2one('external.referential',
                                                   'External Referential',
                                                   required=True,
                                                   readonly=True),
        'line_ids': fields.one2many('external.report.line',
                                    'report_id', 'Report Lines'),
        'failed_line_ids': fields.one2many('external.report.line',
                                           'report_id',
                                           'Failed Report Lines',
                                           domain=[('state', '!=', 'success')]),
        'history_ids': fields.one2many('external.report.history',
                                       'report_id', 'History'),
    }

    def get_report_filter(self, cr, uid, method, object, context=None):
        return [
            ('method', '=', method),
            ('res_id', '=', object.id),
            ('object_related', '=', object._name),
        ]       
                   

    def get_report(self, cr, uid, method, object, context=None):
        report_id = False
        filter = self.get_report_filter(cr, uid, method, object, context=context)
        report = self.search(cr, uid, filter, context=context)
        if report:
            report_id = report[0]
        return report_id

    def _clean_successful_lines(self, cr, uid, report_id, context=None):
        lines_obj = self.pool.get('external.report.line')
        line_ids = lines_obj.search(cr, uid,
                                    [('report_id', '=', report_id),
                                    ('state', '=', 'success')],
                                    context=context)
        lines_obj.unlink(cr, uid, line_ids, context=context)
        return True

    def retry_failed_lines(self, cr, uid, ids, context=None):
        retry_cr = pooler.get_db(cr.dbname).cursor()
        logging.getLogger('external_synchro').info("retry the failed lines of the reports ids %s" % (ids,))
        if isinstance(ids, int):
            ids = [ids]
        if not context:
            context={}
        context['origin'] = 'retry'
        for report in self.read(retry_cr, uid, ids, ['failed_line_ids'], context=context):
            failed_line_ids = report['failed_line_ids']
            if failed_line_ids:
                context['report_id'] = report['id']
                self.start_report(retry_cr, uid, report['id'], context=context)
                self.pool.get('external.report.line').retry(retry_cr, uid, failed_line_ids, context=context)
                self.end_report(retry_cr, uid, report['id'], context=context)
        retry_cr.commit()
        retry_cr.close()
        return True

    def _prepare_start_report(self, cr, uid, method, object, context=None):
        return {'name': method.replace('_', ' ').strip(),
                'object_name': getattr(object, object._rec_name),
                'object_related': object._name,
                'object_related_description': object._description,
                'res_id': object.id,
                'method': method,
                'referential_id': object.referential_id.id,
                'start_date': time.strftime(DEFAULT_SERVER_DATETIME_FORMAT),}

    def start_report(self, cr, uid, id=None, method=None,
                     object=None, context=None):
        """ Start a report, use the report with the id in the parameter
        if given. Otherwise, try to find the report which have the same method
         and object (we use the same report to avoid a
         multiplication of reports) If nothing is found, it create a new report
        """

        if not id and (not method or not object):
            raise Exception('No reference to create the report!')
        if id:
            report_id = id
        else:
            report_id = self.get_report(cr, uid, method, object, context)
                                               
        log_cr = pooler.get_db(cr.dbname).cursor()
        try:
            if report_id:
                print 'report already exist'
                self.write(log_cr, uid, report_id,
                           {'start_date': time.strftime(DEFAULT_SERVER_DATETIME_FORMAT),
                            'end_date': False},
                           context=context)
                # clean successful lines of the last report
                self._clean_successful_lines(log_cr, uid, report_id, context)
            else:
                print 'create report'
                report_id = self.create(
                    log_cr, uid,
                    self._prepare_start_report(
                        cr, uid, method, object, context=context),
                    context=context)
            print 'commit'
            log_cr.commit()

        finally:
            log_cr.close()

        return report_id

    def end_report(self, cr, uid, id, context=None):
        """ Create history lines based on lines
        Successful lines are cleaned at each start of a report
        so we historize their aggregation.
        """
        report = self.browse(cr, uid, id, context=context)
        lines_obj = self.pool.get('external.report.line')
        history_obj = self.pool.get('external.report.history')
        log_cr = pooler.get_db(cr.dbname).cursor()
        try:
            line_ids = lines_obj.search(log_cr, uid,
                                     [('report_id', '=', id),
                                     '|', ('write_date', '>', report.start_date),
                                     ('create_date', '>', report.start_date)],
                                     context=context)

            grouped_lines = lines_obj.aggregate_actions(log_cr, uid,
                                                        line_ids,
                                                        context)

            for line in grouped_lines:
                history_obj.create(log_cr, uid,
                                   {
                        'report_id': id,
                        'res_model': line[1],
                        'action': line[2],
                        'count': grouped_lines[line],
                        'user_id': uid,
                        'state': line[0],
                        'origin': context.get('origin', False),
                    }, context=context)

            self.write(log_cr, uid, id,
                       {'end_date': time.strftime(DEFAULT_SERVER_DATETIME_FORMAT)},
                       context=context)

            log_cr.commit()
        finally:
            log_cr.close()
        return id

external_report()


class external_report_history(osv.osv):
    _name = 'external.report.history'
    _description = 'External Report History'
    _rec_name = 'report_id'
    _order = 'date desc'

    _columns = {
        'report_id': fields.many2one('external.report',
                                              'External Report',
                                              required=True,
                                              readonly=True,
                                              ondelete='cascade'),
        'date': fields.datetime('End Date', required=True, readonly=True),
        'res_model': fields.char('Resource Object', size=64,
                                 required=True, readonly=True),
        'action': fields.char('Action', size=64, required=True, readonly=True),
        'count': fields.integer('Count', readonly=True),
        'user_id': fields.many2one('res.users', 'User', required=True, readonly=True),
        'state': fields.selection((('success', 'Success'),
                                   ('fail', 'Failed')),
                                   'Status', required=True, readonly=True),
        'origin': fields.char('Origin', size=64, readonly=True),
    }

    _defaults = {
        "date": lambda *a: time.strftime(DEFAULT_SERVER_DATETIME_FORMAT)
    }

external_report_history()


class external_report_lines(osv.osv):
    _name = 'external.report.line'
    _description = 'External Report Lines'
    _rec_name = 'res_id'
    _order = 'date desc'

    def _get_data_record(self, cr, uid, ids, field_name, arg, context=None):
        res = {}
        for report_line in self.browse(cr, uid, ids, context=context):
            res[report_line.id] = simplejson.dumps(report_line.data_record)
        return res

    def _set_data_record(self, cr, uid, ids, field_name, arg, context=None):
        res = {}
        for report_line in self.browse(cr, uid, ids, context=context):
            res[report_line.id] = simplejson.loads(report_line.data_record)
        return res

    _columns = {
        'report_id': fields.many2one('external.report',
                                              'External Report',
                                              required=True,
                                              readonly=True,
                                              ondelete='restrict'),
        'state': fields.selection((('success', 'Success'),
                                   ('fail', 'Failed')),
                                   'Status', required=True, readonly=True),
        'res_model': fields.char('Resource Object', size=64,
                                 required=True, readonly=True),
        'res_id': fields.integer('Resource Id', readonly=True),
        'action': fields.char('Action', size=32, required=True, readonly=True),
        'date': fields.datetime('Date', required=True, readonly=True),
        'external_id': fields.char('External ID', size=64, readonly=True),
        'error_message': fields.text('Error Message', readonly=True),
        'data_record': fields.serialized('External Data', readonly=True),
        'data_record_text':fields.function(_get_data_record, fnct_inv=_set_data_record, type="text", string='External Data'),
        'origin_defaults': fields.serialized('Defaults', readonly=True),
        'origin_context': fields.serialized('Context', readonly=True),
    }

    _defaults = {
        "date": lambda *a: time.strftime(DEFAULT_SERVER_DATETIME_FORMAT)
    }

    def _log_base(self, cr, uid, model, action, state=None, res_id=None,
                  external_id=None,exception=None, data_record=None,
                  defaults=None, context=None):
        defaults = defaults or {}
        context = context or {}
        existing_line_id = context.get('retry_report_line_id', False)

        # We do not log any action if no report is started
        # if the log was a fail, we raise to not let the import continue
        # This ensure a backward compatibility, synchro will continue to
        # work exactly the same way if no report is started
        if not(existing_line_id or context.get('report_id', False)):
            if state == 'fail':
                raise
            return False

        report_id = context['report_id']
        log_cr = pooler.get_db(cr.dbname).cursor()

        try:
            origin_defaults = defaults.copy()
            origin_context = context.copy()
            # connection object can not be kept in text indeed
            # FIXME : see if we have some problem with other objects
            # and maybe remove from the conect all objects
            # which are not string, boolean, list, dict, integer, float or ?
            if origin_context.get('conn_obj', False):
                del origin_context['conn_obj']
            if existing_line_id:
                self.write(log_cr, uid,
                               existing_line_id,
                               {'state': state,
                                'date': time.strftime(DEFAULT_SERVER_DATETIME_FORMAT),
                                'error_message': exception and str(exception) or False,
                                'origin_defaults': origin_defaults,
                                'origin_context': origin_context,
                                })
            else:
                existing_line_id = self.create(log_cr, uid, {
                                'report_id': report_id,
                                'state': state,
                                'res_model': model,
                                'action': action,
                                'date': time.strftime(DEFAULT_SERVER_DATETIME_FORMAT),
                                'res_id': res_id,
                                'external_id': external_id,
                                'error_message': exception and str(exception) or False,
                                'data_record': data_record,
                                'origin_defaults': origin_defaults,
                                'origin_context': origin_context,
                            })
                print existing_line_id
            log_cr.commit()

        finally:
            log_cr.close()
        return existing_line_id



#Deprecated
    def log_failed(self, cr, uid, model, action,
                   res_id=None, external_id=None, exception=None,
                   data_record=None, defaults=None, context=None):
        return self._log_base(cr, uid, model, action, 'fail', res_id=res_id,
                             external_id=external_id, exception=exception,
                             data_record=data_record, defaults=defaults,
                             context=context)
#Deprecated
    def log_success(self, cr, uid, model, action,
                    res_id=None, external_id=None, exception=None,
                    data_record=None, defaults=None, context=None):
        return self._log_base(cr, uid,  model, action, 'success', res_id=res_id,
                             external_id=external_id, exception=exception,
                             data_record=data_record, defaults=defaults,
                             context=context)

    def retry(self, cr, uid, ids, context=None):
        if isinstance(ids, int):
            ids = [ids]

        for log in self.browse(cr, uid, ids, context=context):
            mapping = self.pool.get(log.res_model).\
            report_action_mapping(cr, uid, context=context)

            method = mapping.get(log.action, False)
            if not method:
                raise Exception("No python method defined for action %s" %
                                (log.action,))
                                
            
            kwargs={}
            for field, value in method['fields'].items():
                kwargs[field] = safe_eval(value, {'log': log, 'self': self})
                
            if not kwargs.get('context', False):
                kwargs['context']={}
            
            # keep the id of the line to update it with the result
            kwargs['context']['retry_report_line_id'] = log.id
            # force export of the resource
            kwargs['context']['force_export'] = True
            kwargs['context']['force'] = True
            
            ##TODO remove : not needed since magento 6.1 ########
            kwargs['context']['do_not_update_date'] = True         #
            #####################################################
        
            method['method'](cr, uid, **kwargs)
        return True

    def aggregate_actions(self, cr, uid, ids, context=None):
        res = {}
        for line in self.browse(cr, uid, ids, context=context):
            state = line.state
            model = line.res_model
            action = line.action

            if not res.get((state, model, action), False):
                res[(state, model, action)] = 0
            res[(state, model, action)] += 1

        return res

external_report_lines()
